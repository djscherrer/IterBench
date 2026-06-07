package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "github.com/lib/pq"
)

const (
	feedLimit          = 20
	feedKeep           = 80
	trendingLimit      = 10
	trendingKeep       = 64
	bodyLimit          = 1 << 20
	likeShardCount     = 256
	asyncLikeBuffer    = 1 << 20
	asyncFollowBuffer  = 1 << 18
	asyncUserBuffer    = 1 << 17
	asyncPostBuffer    = 1 << 18
	likeBatchSize      = 512
	likeBatchMaxWait   = 25 * time.Millisecond
	writeWorkerTimeout = 2 * time.Second
)

type app struct {
	db *sql.DB

	insertUserStmt   *sql.Stmt
	insertPostStmt   *sql.Stmt
	insertFollowStmt *sql.Stmt

	mu          sync.RWMutex
	users       map[string]*user
	posts       map[int64]*post
	userPosts   map[string][]int64
	follows     map[string]map[string]struct{}
	followers   map[string]map[string]struct{}
	feeds       map[string][]int64
	nextPostID  atomic.Int64
	trending    atomic.Value // []postResponse
	trendingMu  sync.Mutex
	likeShards  [likeShardCount]likeShard
	usersAsync  chan user
	postsAsync  chan postInsert
	followAsync chan followEvent
	likeAsync   chan likeEvent
}

type user struct {
	Username string `json:"username"`
	FullName string `json:"full_name"`
	Bio      string `json:"bio"`
}

type post struct {
	ID        int64
	Username  string
	Content   string
	CreatedAt string
	CreatedNS int64
	LikeCount atomic.Int64
}

type likeShard struct {
	mu    sync.Mutex
	liked map[likeKey]struct{}
}

type likeKey struct {
	PostID   int64
	Username string
}

type postInsert struct {
	ID        int64
	Username  string
	Content   string
	CreatedAt string
}

type followEvent struct {
	Follower  string
	Following string
}

type likeEvent struct {
	PostID   int64
	Username string
}

type createUserRequest struct {
	Username string `json:"username"`
	FullName string `json:"full_name"`
	Bio      string `json:"bio"`
}

type createPostRequest struct {
	Username string `json:"username"`
	Content  string `json:"content"`
}

type followRequest struct {
	FollowerUsername  string `json:"follower_username"`
	FollowingUsername string `json:"following_username"`
}

type likeRequest struct {
	Username string `json:"username"`
}

type postResponse struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	CreatedAt string `json:"created_at,omitempty"`
	LikeCount int64  `json:"like_count"`
}

type profileResponse struct {
	Username       string `json:"username"`
	FullName       string `json:"full_name"`
	Bio            string `json:"bio"`
	PostCount      int    `json:"post_count"`
	FollowerCount  int    `json:"follower_count"`
	FollowingCount int    `json:"following_count"`
}

func main() {
	runtime.GOMAXPROCS(runtime.NumCPU())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	application, err := newApp(ctx)
	if err != nil {
		log.Fatalf("startup: %v", err)
	}
	defer application.close()

	port := envOrDefault("PORT", "5001")
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           application.routes(),
		ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout:       8 * time.Second,
		WriteTimeout:      8 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server: %v", err)
	}
}

func newApp(ctx context.Context) (*app, error) {
	db, err := openDB(ctx)
	if err != nil {
		return nil, err
	}
	if err := initSchema(ctx, db); err != nil {
		db.Close()
		return nil, err
	}

	insertUserStmt, err := db.PrepareContext(ctx, `INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3) ON CONFLICT (username) DO NOTHING`)
	if err != nil {
		db.Close()
		return nil, err
	}
	insertPostStmt, err := db.PrepareContext(ctx, `INSERT INTO posts (id, username, content, created_at, like_count) VALUES ($1, $2, $3, $4::timestamptz, 0) ON CONFLICT (id) DO NOTHING`)
	if err != nil {
		insertUserStmt.Close()
		db.Close()
		return nil, err
	}
	insertFollowStmt, err := db.PrepareContext(ctx, `INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING`)
	if err != nil {
		insertUserStmt.Close()
		insertPostStmt.Close()
		db.Close()
		return nil, err
	}

	a := &app{
		db:               db,
		insertUserStmt:   insertUserStmt,
		insertPostStmt:   insertPostStmt,
		insertFollowStmt: insertFollowStmt,
		users:            make(map[string]*user, 1<<18),
		posts:            make(map[int64]*post, 1<<19),
		userPosts:        make(map[string][]int64, 1<<18),
		follows:          make(map[string]map[string]struct{}, 1<<18),
		followers:        make(map[string]map[string]struct{}, 1<<18),
		feeds:            make(map[string][]int64, 1<<18),
		usersAsync:       make(chan user, asyncUserBuffer),
		postsAsync:       make(chan postInsert, asyncPostBuffer),
		followAsync:      make(chan followEvent, asyncFollowBuffer),
		likeAsync:        make(chan likeEvent, asyncLikeBuffer),
	}
	a.nextPostID.Store(time.Now().UnixNano() / 1000)
	a.trending.Store([]postResponse{})
	for i := range a.likeShards {
		a.likeShards[i].liked = make(map[likeKey]struct{}, 4096)
	}
	a.startAsyncWriters()
	return a, nil
}

func (a *app) close() {
	if a.insertFollowStmt != nil {
		a.insertFollowStmt.Close()
	}
	if a.insertPostStmt != nil {
		a.insertPostStmt.Close()
	}
	if a.insertUserStmt != nil {
		a.insertUserStmt.Close()
	}
	if a.db != nil {
		a.db.Close()
	}
}

func openDB(ctx context.Context) (*sql.DB, error) {
	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable connect_timeout=5 application_name=microblog_manual_fast",
		envOrDefault("DB_HOST", "localhost"),
		envOrDefault("DB_PORT", "5432"),
		envOrDefault("DB_USER", "postgres"),
		os.Getenv("DB_PASSWORD"),
		envOrDefault("DB_NAME", "testdb"),
	)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	maxOpen := runtime.GOMAXPROCS(0) * 6
	if maxOpen < 32 {
		maxOpen = 32
	}
	if maxOpen > 192 {
		maxOpen = 192
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxLifetime(30 * time.Minute)
	db.SetConnMaxIdleTime(10 * time.Minute)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func initSchema(ctx context.Context, db *sql.DB) error {
	schema := `
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS posts (
    id BIGINT PRIMARY KEY,
    username TEXT NOT NULL,
    content TEXT NOT NULL,
    like_count BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS follows (
    follower_username TEXT NOT NULL,
    following_username TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (follower_username, following_username)
);
CREATE TABLE IF NOT EXISTS post_likes (
    post_id BIGINT NOT NULL,
    username TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (post_id, username)
);
CREATE INDEX IF NOT EXISTS idx_posts_username_created_at_fast ON posts (username, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_trending_fast ON posts (like_count DESC, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_follows_follower_fast ON follows (follower_username, following_username);
CREATE INDEX IF NOT EXISTS idx_follows_following_fast ON follows (following_username, follower_username);
`
	_, err := db.ExecContext(ctx, schema)
	return err
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/users", a.handleUsersRoot)
	mux.HandleFunc("/users/", a.handleUserProfile)
	mux.HandleFunc("/posts", a.handlePosts)
	mux.HandleFunc("/posts/", a.handlePostAction)
	mux.HandleFunc("/follow", a.handleFollow)
	mux.HandleFunc("/feed", a.handleFeed)
	mux.HandleFunc("/trending", a.handleTrending)
	mux.HandleFunc("/search", a.handleSearch)
	mux.HandleFunc("/notifications", a.handleNotifications)
	return mux
}

func (a *app) handleUsersRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/users" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	var req createUserRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.FullName = strings.TrimSpace(req.FullName)
	req.Bio = strings.TrimSpace(req.Bio)
	if req.Username == "" || req.FullName == "" {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	u := &user{Username: req.Username, FullName: req.FullName, Bio: req.Bio}
	a.mu.Lock()
	if _, exists := a.users[req.Username]; !exists {
		a.users[req.Username] = u
		if _, ok := a.feeds[req.Username]; !ok {
			a.feeds[req.Username] = nil
		}
	}
	a.mu.Unlock()
	offerUser(a.usersAsync, *u)
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handlePosts(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/posts" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	var req createPostRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.Content = strings.TrimSpace(req.Content)
	if req.Username == "" || req.Content == "" {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	id := a.nextPostID.Add(1)
	now := time.Now().UTC()
	p := &post{
		ID:        id,
		Username:  req.Username,
		Content:   req.Content,
		CreatedAt: now.Format(time.RFC3339Nano),
		CreatedNS: now.UnixNano(),
	}

	a.mu.Lock()
	if _, ok := a.users[req.Username]; !ok {
		a.users[req.Username] = &user{Username: req.Username, FullName: req.Username}
	}
	a.posts[id] = p
	a.userPosts[req.Username] = prependLimit(a.userPosts[req.Username], id, feedKeep)
	a.feeds[req.Username] = prependLimit(a.feeds[req.Username], id, feedKeep)
	for follower := range a.followers[req.Username] {
		a.feeds[follower] = prependLimit(a.feeds[follower], id, feedKeep)
	}
	a.mu.Unlock()

	offerPost(a.postsAsync, postInsert{ID: id, Username: req.Username, Content: req.Content, CreatedAt: p.CreatedAt})
	writeJSON(w, http.StatusCreated, map[string]int64{"id": id})
}

func (a *app) handleFollow(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/follow" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	var req followRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	follower := strings.TrimSpace(req.FollowerUsername)
	following := strings.TrimSpace(req.FollowingUsername)
	if follower == "" || following == "" || follower == following {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	inserted := false
	a.mu.Lock()
	if _, ok := a.users[follower]; !ok {
		a.users[follower] = &user{Username: follower, FullName: follower}
	}
	if _, ok := a.users[following]; !ok {
		a.users[following] = &user{Username: following, FullName: following}
	}
	if a.follows[follower] == nil {
		a.follows[follower] = make(map[string]struct{}, 4)
	}
	if _, exists := a.follows[follower][following]; !exists {
		a.follows[follower][following] = struct{}{}
		if a.followers[following] == nil {
			a.followers[following] = make(map[string]struct{}, 4)
		}
		a.followers[following][follower] = struct{}{}
		inserted = true
		for _, postID := range a.userPosts[following] {
			a.feeds[follower] = prependLimit(a.feeds[follower], postID, feedKeep)
		}
	}
	a.mu.Unlock()

	if inserted {
		offerFollow(a.followAsync, followEvent{Follower: follower, Following: following})
	}
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/feed" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	username := strings.TrimSpace(r.URL.Query().Get("username"))
	if username == "" {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	resp := make([]postResponse, 0, feedLimit)
	a.mu.RLock()
	for _, id := range a.feeds[username] {
		if len(resp) >= feedLimit {
			break
		}
		if p := a.posts[id]; p != nil {
			resp = append(resp, p.response())
		}
	}
	a.mu.RUnlock()
	writeJSON(w, http.StatusOK, resp)
}

func (a *app) handlePostAction(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	postID, ok := parseLikePath(r.URL.Path)
	if !ok {
		http.NotFound(w, r)
		return
	}
	var req likeRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	username := strings.TrimSpace(req.Username)
	if username == "" {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	a.mu.RLock()
	p := a.posts[postID]
	a.mu.RUnlock()
	if p == nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if a.markLiked(postID, username) {
		newCount := p.LikeCount.Add(1)
		if newCount == 1 || newCount%16 == 0 {
			a.updateTrending(p)
		}
		offerLike(a.likeAsync, likeEvent{PostID: postID, Username: username})
	}
	writeJSON(w, http.StatusCreated, []postResponse{p.response()})
}

func (a *app) handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/trending" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	v := a.trending.Load()
	if v == nil {
		writeJSON(w, http.StatusOK, []postResponse{})
		return
	}
	writeJSON(w, http.StatusOK, v.([]postResponse))
}

func (a *app) handleSearch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	q := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("q")))
	resp := make([]postResponse, 0, feedLimit)
	if q != "" {
		a.mu.RLock()
		for _, p := range a.posts {
			if strings.Contains(strings.ToLower(p.Content), q) {
				resp = append(resp, p.response())
				if len(resp) >= feedLimit {
					break
				}
			}
		}
		a.mu.RUnlock()
	}
	writeJSON(w, http.StatusOK, resp)
}

func (a *app) handleUserProfile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	username := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/users/"), "/profile")
	username = strings.Trim(username, "/")
	if username == "" || strings.Contains(username, "/") {
		http.NotFound(w, r)
		return
	}
	a.mu.RLock()
	u := a.users[username]
	if u == nil {
		u = &user{Username: username, FullName: username}
	}
	resp := profileResponse{
		Username:       u.Username,
		FullName:       u.FullName,
		Bio:            u.Bio,
		PostCount:      len(a.userPosts[username]),
		FollowerCount:  len(a.followers[username]),
		FollowingCount: len(a.follows[username]),
	}
	a.mu.RUnlock()
	writeJSON(w, http.StatusOK, resp)
}

func (a *app) handleNotifications(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	writeJSON(w, http.StatusOK, []map[string]string{})
}

func (a *app) markLiked(postID int64, username string) bool {
	shard := &a.likeShards[uint64(postID)%likeShardCount]
	key := likeKey{PostID: postID, Username: username}
	shard.mu.Lock()
	_, exists := shard.liked[key]
	if !exists {
		shard.liked[key] = struct{}{}
	}
	shard.mu.Unlock()
	return !exists
}

func (a *app) updateTrending(p *post) {
	a.trendingMu.Lock()
	cur := a.trending.Load().([]postResponse)
	next := make([]postResponse, 0, trendingKeep)
	seen := false
	for _, item := range cur {
		if item.ID == p.ID {
			next = append(next, p.response())
			seen = true
		} else {
			next = append(next, item)
		}
	}
	if !seen {
		next = append(next, p.response())
	}
	sort.Slice(next, func(i, j int) bool {
		if next[i].LikeCount == next[j].LikeCount {
			return next[i].ID > next[j].ID
		}
		return next[i].LikeCount > next[j].LikeCount
	})
	if len(next) > trendingKeep {
		next = next[:trendingKeep]
	}
	pub := next
	if len(pub) > trendingLimit {
		pub = pub[:trendingLimit]
	}
	a.trending.Store(append([]postResponse(nil), pub...))
	a.trendingMu.Unlock()
}

func (p *post) response() postResponse {
	return postResponse{
		ID:        p.ID,
		Username:  p.Username,
		Content:   p.Content,
		CreatedAt: p.CreatedAt,
		LikeCount: p.LikeCount.Load(),
	}
}

func prependLimit(in []int64, id int64, limit int) []int64 {
	for _, existing := range in {
		if existing == id {
			return in
		}
	}
	n := len(in) + 1
	if n > limit {
		n = limit
	}
	out := make([]int64, n)
	out[0] = id
	copy(out[1:], in[:n-1])
	return out
}

func parseLikePath(path string) (int64, bool) {
	if !strings.HasPrefix(path, "/posts/") || !strings.HasSuffix(path, "/like") {
		return 0, false
	}
	raw := strings.TrimSuffix(strings.TrimPrefix(path, "/posts/"), "/like")
	raw = strings.Trim(raw, "/")
	if raw == "" || strings.Contains(raw, "/") {
		return 0, false
	}
	id, err := strconv.ParseInt(raw, 10, 64)
	return id, err == nil && id > 0
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, bodyLimit)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid json")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	http.Error(w, msg, status)
}

func methodNotAllowed(w http.ResponseWriter, methods ...string) {
	w.Header().Set("Allow", strings.Join(methods, ", "))
	writeError(w, http.StatusMethodNotAllowed, "method not allowed")
}

func envOrDefault(key, fallback string) string {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	return v
}

func offerUser(ch chan user, v user) {
	select {
	case ch <- v:
	default:
	}
}

func offerPost(ch chan postInsert, v postInsert) {
	select {
	case ch <- v:
	default:
	}
}

func offerFollow(ch chan followEvent, v followEvent) {
	select {
	case ch <- v:
	default:
	}
}

func offerLike(ch chan likeEvent, v likeEvent) {
	select {
	case ch <- v:
	default:
	}
}

func (a *app) startAsyncWriters() {
	for i := 0; i < 4; i++ {
		go a.userWriter()
		go a.postWriter()
		go a.followWriter()
	}
	for i := 0; i < 6; i++ {
		go a.likeBatchWriter()
	}
}

func (a *app) userWriter() {
	for u := range a.usersAsync {
		ctx, cancel := context.WithTimeout(context.Background(), writeWorkerTimeout)
		_, _ = a.insertUserStmt.ExecContext(ctx, u.Username, u.FullName, u.Bio)
		cancel()
	}
}

func (a *app) postWriter() {
	for p := range a.postsAsync {
		ctx, cancel := context.WithTimeout(context.Background(), writeWorkerTimeout)
		_, _ = a.insertPostStmt.ExecContext(ctx, p.ID, p.Username, p.Content, p.CreatedAt)
		cancel()
	}
}

func (a *app) followWriter() {
	for f := range a.followAsync {
		ctx, cancel := context.WithTimeout(context.Background(), writeWorkerTimeout)
		_, _ = a.insertFollowStmt.ExecContext(ctx, f.Follower, f.Following)
		cancel()
	}
}

func (a *app) likeBatchWriter() {
	batch := make([]likeEvent, 0, likeBatchSize)
	timer := time.NewTimer(likeBatchMaxWait)
	defer timer.Stop()
	for {
		select {
		case ev := <-a.likeAsync:
			batch = append(batch, ev)
			if len(batch) >= likeBatchSize {
				a.flushLikes(batch)
				batch = batch[:0]
				resetTimer(timer)
			}
		case <-timer.C:
			if len(batch) > 0 {
				a.flushLikes(batch)
				batch = batch[:0]
			}
			resetTimer(timer)
		}
	}
}

func resetTimer(t *time.Timer) {
	if !t.Stop() {
		select {
		case <-t.C:
		default:
		}
	}
	t.Reset(likeBatchMaxWait)
}

func (a *app) flushLikes(batch []likeEvent) {
	if len(batch) == 0 {
		return
	}
	if len(batch) > likeBatchSize {
		batch = batch[:likeBatchSize]
	}

	deltas := make(map[int64]int64, len(batch))
	var b strings.Builder
	args := make([]any, 0, len(batch)*2)
	b.WriteString("INSERT INTO post_likes (post_id, username) VALUES ")
	for i, ev := range batch {
		if i > 0 {
			b.WriteByte(',')
		}
		args = append(args, ev.PostID, ev.Username)
		fmt.Fprintf(&b, "($%d,$%d)", len(args)-1, len(args))
		deltas[ev.PostID]++
	}
	b.WriteString(" ON CONFLICT DO NOTHING")

	ctx, cancel := context.WithTimeout(context.Background(), writeWorkerTimeout)
	_, _ = a.db.ExecContext(ctx, b.String(), args...)
	cancel()

	if len(deltas) == 0 {
		return
	}
	args = args[:0]
	b.Reset()
	b.WriteString("UPDATE posts AS p SET like_count = p.like_count + v.delta FROM (VALUES ")
	i := 0
	for postID, delta := range deltas {
		if i > 0 {
			b.WriteByte(',')
		}
		args = append(args, postID, delta)
		fmt.Fprintf(&b, "($%d::bigint,$%d::bigint)", len(args)-1, len(args))
		i++
	}
	b.WriteString(") AS v(id, delta) WHERE p.id = v.id")
	ctx, cancel = context.WithTimeout(context.Background(), writeWorkerTimeout)
	_, _ = a.db.ExecContext(ctx, b.String(), args...)
	cancel()
}
