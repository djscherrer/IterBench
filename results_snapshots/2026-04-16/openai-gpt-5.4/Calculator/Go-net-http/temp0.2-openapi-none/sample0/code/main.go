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

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/calculator", calculatorHandler)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := "0.0.0.0:" + port
	log.Printf("listening on %s", addr)

	server := &http.Server{
		Addr:    addr,
		Handler: mux,
	}

	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
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

	var req calculateRequest
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()

	if err := decoder.Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}

	if strings.TrimSpace(req.Expression) == "" {
		http.Error(w, "expression is required", http.StatusBadRequest)
		return
	}

	result, err := evalExpression(req.Expression)
	if err != nil {
		http.Error(w, "invalid expression: "+err.Error(), http.StatusBadRequest)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	resp := calculateResponse{
		Result: formatFloat(result),
	}
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		http.Error(w, "failed to encode response", http.StatusInternalServerError)
		return
	}
}

func evalExpression(input string) (float64, error) {
	p := &parser{input: input}
	value, err := p.parseExpression()
	if err != nil {
		return 0, err
	}

	p.skipSpaces()
	if !p.isAtEnd() {
		return 0, fmt.Errorf("unexpected character '%c'", p.current())
	}

	return value, nil
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
		if p.isAtEnd() {
			return left, nil
		}

		switch p.current() {
		case '+':
			p.pos++
			right, err := p.parseTerm()
			if err != nil {
				return 0, err
			}
			left += right
		case '-':
			p.pos++
			right, err := p.parseTerm()
			if err != nil {
				return 0, err
			}
			left -= right
		default:
			return left, nil
		}
	}
}

func (p *parser) parseTerm() (float64, error) {
	left, err := p.parseFactor()
	if err != nil {
		return 0, err
	}

	for {
		p.skipSpaces()
		if p.isAtEnd() {
			return left, nil
		}

		switch p.current() {
		case '*':
			p.pos++
			right, err := p.parseFactor()
			if err != nil {
				return 0, err
			}
			left *= right
		case '/':
			p.pos++
			right, err := p.parseFactor()
			if err != nil {
				return 0, err
			}
			if right == 0 {
				return 0, errors.New("division by zero")
			}
			left /= right
		default:
			return left, nil
		}
	}
}

func (p *parser) parseFactor() (float64, error) {
	p.skipSpaces()
	if p.isAtEnd() {
		return 0, errors.New("unexpected end of expression")
	}

	switch p.current() {
	case '+':
		p.pos++
		return p.parseFactor()
	case '-':
		p.pos++
		value, err := p.parseFactor()
		if err != nil {
			return 0, err
		}
		return -value, nil
	case '(':
		p.pos++
		value, err := p.parseExpression()
		if err != nil {
			return 0, err
		}
		p.skipSpaces()
		if p.isAtEnd() || p.current() != ')' {
			return 0, errors.New("missing closing parenthesis")
		}
		p.pos++
		return value, nil
	default:
		return p.parseNumber()
	}
}

func (p *parser) parseNumber() (float64, error) {
	p.skipSpaces()
	start := p.pos
	dotSeen := false
	digitSeen := false

	for !p.isAtEnd() {
		ch := p.current()
		if unicode.IsDigit(rune(ch)) {
			digitSeen = true
			p.pos++
			continue
		}
		if ch == '.' && !dotSeen {
			dotSeen = true
			p.pos++
			continue
		}
		break
	}

	if !digitSeen {
		if p.isAtEnd() {
			return 0, errors.New("expected number")
		}
		return 0, fmt.Errorf("unexpected character '%c'", p.current())
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

func (p *parser) isAtEnd() bool {
	return p.pos >= len(p.input)
}

func (p *parser) current() byte {
	return p.input[p.pos]
}

func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
}