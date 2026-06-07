package main

import (
	"encoding/json"
	"errors"
	"log"
	"math"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	maxBodyBytes  = 1 << 20
	maxVectorDim  = 128
	defaultSlots  = 5
	maxSlots      = 20
	scoreScale    = 100.0
	readTimeout   = 5 * time.Second
	writeTimeout  = 8 * time.Second
	idleTimeout   = 60 * time.Second
	wildcardIndex = "*"
)

type app struct {
	mu              sync.RWMutex
	campaigns       map[string]*campaign
	byCountry       map[string][]*campaign
	wildcardCountry []*campaign
}

type campaign struct {
	ID              string
	Advertiser      string
	BidCents        float64
	QualityScore    float64
	Countries       map[string]struct{}
	Interests       map[string]struct{}
	Vector          []float64
	baseScore       float64
	impressions     atomic.Int64
	clicks          atomic.Int64
	conversions     atomic.Int64
	targetsAnyCntry bool
	targetsAnyIntr  bool
}

type createCampaignRequest struct {
	CampaignID      string    `json:"campaign_id"`
	Advertiser      string    `json:"advertiser"`
	BidCents        float64   `json:"bid_cents"`
	QualityScore    float64   `json:"quality_score"`
	TargetCountries []string  `json:"target_countries"`
	TargetInterests []string  `json:"target_interests"`
	Embedding       []float64 `json:"embedding"`
}

type campaignCreatedResponse struct {
	CampaignID string `json:"campaign_id"`
}

type auctionRequest struct {
	UserID    string    `json:"user_id"`
	Country   string    `json:"country"`
	Interests []string  `json:"interests"`
	Embedding []float64 `json:"embedding"`
	Slots     int       `json:"slots"`
}

type auctionResponse struct {
	Winners []winnerResponse `json:"winners"`
}

type winnerResponse struct {
	CampaignID string  `json:"campaign_id"`
	Advertiser string  `json:"advertiser"`
	Score      float64 `json:"score"`
	BidCents   float64 `json:"bid_cents"`
}

type eventRequest struct {
	Type string `json:"type"`
}

type statsResponse struct {
	CampaignID  string `json:"campaign_id"`
	Impressions int64  `json:"impressions"`
	Clicks      int64  `json:"clicks"`
	Conversions int64  `json:"conversions"`
}

type scoredCampaign struct {
	campaign *campaign
	score    float64
}

func main() {
	a := &app{
		campaigns: make(map[string]*campaign, 1<<14),
		byCountry: make(map[string][]*campaign, 64),
	}

	server := &http.Server{
		Addr:         "0.0.0.0:" + envOrDefault("PORT", "5001"),
		Handler:      a.routes(),
		ReadTimeout:  readTimeout,
		WriteTimeout: writeTimeout,
		IdleTimeout:  idleTimeout,
	}
	log.Printf("AdAuction listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/campaigns", a.handleCampaigns)
	mux.HandleFunc("/campaigns/", a.handleCampaignPath)
	mux.HandleFunc("/auction", a.handleAuction)
	return mux
}

func (a *app) handleCampaigns(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req createCampaignRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	c, err := buildCampaign(req)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	a.mu.Lock()
	if _, exists := a.campaigns[c.ID]; exists {
		a.mu.Unlock()
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "duplicate campaign"})
		return
	}
	a.campaigns[c.ID] = c
	if c.targetsAnyCntry {
		a.wildcardCountry = append(a.wildcardCountry, c)
	} else {
		for country := range c.Countries {
			a.byCountry[country] = append(a.byCountry[country], c)
		}
	}
	a.mu.Unlock()

	writeJSON(w, http.StatusCreated, campaignCreatedResponse{CampaignID: c.ID})
}

func (a *app) handleAuction(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req auctionRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	country := normalizeCountry(req.Country)
	if clean(req.UserID) == "" || country == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid auction"})
		return
	}
	query, err := normalizeVector(req.Embedding)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	slots := req.Slots
	if slots <= 0 {
		slots = defaultSlots
	}
	if slots > maxSlots {
		slots = maxSlots
	}
	interests := normalizeSet(req.Interests, normalizeInterest)

	candidates := a.candidatesForCountry(country)
	top := make([]scoredCampaign, 0, slots)
	for _, c := range candidates {
		if len(c.Vector) != len(query) || !c.matchesInterests(interests) {
			continue
		}
		score := c.baseScore + scoreScale*dot(c.Vector, query)
		if len(top) < slots {
			top = append(top, scoredCampaign{campaign: c, score: score})
			continue
		}
		minIdx := 0
		minScore := top[0].score
		for i := 1; i < len(top); i++ {
			if top[i].score < minScore {
				minIdx = i
				minScore = top[i].score
			}
		}
		if score > minScore || (score == minScore && c.ID < top[minIdx].campaign.ID) {
			top[minIdx] = scoredCampaign{campaign: c, score: score}
		}
	}

	sort.Slice(top, func(i, j int) bool {
		if top[i].score == top[j].score {
			return top[i].campaign.ID < top[j].campaign.ID
		}
		return top[i].score > top[j].score
	})

	winners := make([]winnerResponse, len(top))
	for i, sc := range top {
		sc.campaign.impressions.Add(1)
		winners[i] = winnerResponse{
			CampaignID: sc.campaign.ID,
			Advertiser: sc.campaign.Advertiser,
			Score:      math.Round(sc.score*1000) / 1000,
			BidCents:   sc.campaign.BidCents,
		}
	}
	writeJSON(w, http.StatusOK, auctionResponse{Winners: winners})
}

func (a *app) handleCampaignPath(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/campaigns/")
	parts := strings.Split(strings.Trim(rest, "/"), "/")
	if len(parts) != 2 || parts[0] == "" {
		http.NotFound(w, r)
		return
	}
	id := parts[0]
	switch parts[1] {
	case "events":
		a.handleEvent(w, r, id)
	case "stats":
		a.handleStats(w, r, id)
	default:
		http.NotFound(w, r)
	}
}

func (a *app) handleEvent(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req eventRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	c := a.getCampaign(id)
	if c == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "campaign not found"})
		return
	}
	switch normalizeInterest(req.Type) {
	case "click":
		c.clicks.Add(1)
	case "conversion":
		c.conversions.Add(1)
	default:
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid event"})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"status": "recorded"})
}

func (a *app) handleStats(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	c := a.getCampaign(id)
	if c == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "campaign not found"})
		return
	}
	writeJSON(w, http.StatusOK, statsResponse{
		CampaignID:  c.ID,
		Impressions: c.impressions.Load(),
		Clicks:      c.clicks.Load(),
		Conversions: c.conversions.Load(),
	})
}

func (a *app) getCampaign(id string) *campaign {
	a.mu.RLock()
	c := a.campaigns[id]
	a.mu.RUnlock()
	return c
}

func (a *app) candidatesForCountry(country string) []*campaign {
	a.mu.RLock()
	specific := a.byCountry[country]
	wild := a.wildcardCountry
	candidates := make([]*campaign, 0, len(specific)+len(wild))
	candidates = append(candidates, specific...)
	candidates = append(candidates, wild...)
	a.mu.RUnlock()
	return candidates
}

func buildCampaign(req createCampaignRequest) (*campaign, error) {
	id := clean(req.CampaignID)
	advertiser := clean(req.Advertiser)
	if id == "" || advertiser == "" || req.BidCents <= 0 || req.QualityScore < 0 {
		return nil, errors.New("invalid campaign")
	}
	vector, err := normalizeVector(req.Embedding)
	if err != nil {
		return nil, err
	}
	countries := normalizeSet(req.TargetCountries, normalizeCountry)
	interests := normalizeSet(req.TargetInterests, normalizeInterest)
	return &campaign{
		ID:              id,
		Advertiser:      advertiser,
		BidCents:        req.BidCents,
		QualityScore:    req.QualityScore,
		Countries:       countries,
		Interests:       interests,
		Vector:          vector,
		baseScore:       req.BidCents * req.QualityScore,
		targetsAnyCntry: len(countries) == 0,
		targetsAnyIntr:  len(interests) == 0,
	}, nil
}

func (c *campaign) matchesInterests(userInterests map[string]struct{}) bool {
	if c.targetsAnyIntr {
		return true
	}
	for interest := range userInterests {
		if _, ok := c.Interests[interest]; ok {
			return true
		}
	}
	return false
}

func normalizeVector(values []float64) ([]float64, error) {
	if len(values) < 2 || len(values) > maxVectorDim {
		return nil, errors.New("invalid embedding")
	}
	var sum float64
	for _, v := range values {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, errors.New("invalid embedding")
		}
		sum += v * v
	}
	norm := math.Sqrt(sum)
	if norm == 0 {
		return nil, errors.New("invalid embedding")
	}
	out := make([]float64, len(values))
	inv := 1 / norm
	for i, v := range values {
		out[i] = v * inv
	}
	return out, nil
}

func dot(a, b []float64) float64 {
	var total float64
	for i, v := range a {
		total += v * b[i]
	}
	return total
}

func normalizeSet(values []string, fn func(string) string) map[string]struct{} {
	out := make(map[string]struct{}, len(values))
	for _, raw := range values {
		v := fn(raw)
		if v != "" && v != wildcardIndex {
			out[v] = struct{}{}
		}
	}
	return out
}

func normalizeCountry(v string) string {
	return strings.ToUpper(clean(v))
}

func normalizeInterest(v string) string {
	return strings.ToLower(clean(v))
}

func clean(v string) string {
	return strings.TrimSpace(v)
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
