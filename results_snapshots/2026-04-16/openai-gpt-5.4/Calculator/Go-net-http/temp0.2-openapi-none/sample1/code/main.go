package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"unicode"
)

type calculateRequest struct {
	Expression string `json:"expression"`
}

type calculateResponse struct {
	Result string `json:"result"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/calculator", calculatorHandler)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := "0.0.0.0:" + port
	log.Printf("listening on %s", addr)

	if err := http.ListenAndServe(addr, withLogging(mux)); err != nil {
		log.Fatal(err)
	}
}

func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s", r.Method, r.URL.Path)
		next.ServeHTTP(w, r)
	})
}

func calculatorHandler(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/calculator" {
		http.NotFound(w, r)
		return
	}

	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	if ct := r.Header.Get("Content-Type"); ct != "" && !strings.HasPrefix(ct, "application/json") {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "content type must be application/json"})
		return
	}

	defer r.Body.Close()

	var req calculateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "invalid JSON body"})
		return
	}

	if strings.TrimSpace(req.Expression) == "" {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "expression is required"})
		return
	}

	result, err := evaluateExpression(req.Expression)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, calculateResponse{
		Result: formatFloat(result),
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func evaluateExpression(input string) (float64, error) {
	p := &parser{input: input}
	result, err := p.parseExpression()
	if err != nil {
		return 0, err
	}

	p.skipSpaces()
	if !p.isAtEnd() {
		return 0, fmt.Errorf("unexpected character: %q", p.current())
	}

	return result, nil
}

type parser struct {
	input string
	pos   int
}

func (p *parser) parseExpression() (float64, error) {
	left, err := p.parseTerm()
	if err != nil {
		return 0, err
	}

	for {
		p.skipSpaces()
		if p.match('+') {
			right, err := p.parseTerm()
			if err != nil {
				return 0, err
			}
			left += right
		} else if p.match('-') {
			right, err := p.parseTerm()
			if err != nil {
				return 0, err
			}
			left -= right
		} else {
			break
		}
	}

	return left, nil
}

func (p *parser) parseTerm() (float64, error) {
	left, err := p.parseFactor()
	if err != nil {
		return 0, err
	}

	for {
		p.skipSpaces()
		if p.match('*') {
			right, err := p.parseFactor()
			if err != nil {
				return 0, err
			}
			left *= right
		} else if p.match('/') {
			right, err := p.parseFactor()
			if err != nil {
				return 0, err
			}
			if right == 0 {
				return 0, errors.New("division by zero")
			}
			left /= right
		} else {
			break
		}
	}

	return left, nil
}

func (p *parser) parseFactor() (float64, error) {
	p.skipSpaces()

	if p.match('+') {
		return p.parseFactor()
	}

	if p.match('-') {
		value, err := p.parseFactor()
		if err != nil {
			return 0, err
		}
		return -value, nil
	}

	if p.match('(') {
		value, err := p.parseExpression()
		if err != nil {
			return 0, err
		}
		p.skipSpaces()
		if !p.match(')') {
			return 0, errors.New("missing closing parenthesis")
		}
		return value, nil
	}

	return p.parseNumber()
}

func (p *parser) parseNumber() (float64, error) {
	p.skipSpaces()
	start := p.pos
	dotSeen := false

	for !p.isAtEnd() {
		ch := p.current()
		if ch == '.' {
			if dotSeen {
				break
			}
			dotSeen = true
			p.pos++
			continue
		}
		if !unicode.IsDigit(rune(ch)) {
			break
		}
		p.pos++
	}

	if start == p.pos || p.input[start:p.pos] == "." {
		if p.isAtEnd() {
			return 0, errors.New("expected number")
		}
		return 0, fmt.Errorf("unexpected character: %q", p.current())
	}

	value, err := strconv.ParseFloat(p.input[start:p.pos], 64)
	if err != nil {
		return 0, errors.New("invalid number")
	}

	return value, nil
}

func (p *parser) skipSpaces() {
	for !p.isAtEnd() && unicode.IsSpace(rune(p.current())) {
		p.pos++
	}
}

func (p *parser) match(ch byte) bool {
	p.skipSpaces()
	if p.isAtEnd() || p.current() != ch {
		return false
	}
	p.pos++
	return true
}

func (p *parser) current() byte {
	return p.input[p.pos]
}

func (p *parser) isAtEnd() bool {
	return p.pos >= len(p.input)
}

func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
}