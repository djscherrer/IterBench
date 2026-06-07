package main

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"unicode"
)

type calculatorRequest struct {
	Expression string `json:"expression"`
}

type calculatorResponse struct {
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
	log.Printf("server listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
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

	if r.Body == nil {
		writeJSONError(w, http.StatusBadRequest, "request body is required")
		return
	}
	defer r.Body.Close()

	var req calculatorRequest
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}

	if strings.TrimSpace(req.Expression) == "" {
		writeJSONError(w, http.StatusBadRequest, "expression is required")
		return
	}

	result, err := evaluateExpression(req.Expression)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, err.Error())
		return
	}

	writeJSON(w, http.StatusOK, calculatorResponse{
		Result: formatFloat(result),
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeJSONError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

func evaluateExpression(input string) (float64, error) {
	p := &parser{input: input}
	result, err := p.parseExpression()
	if err != nil {
		return 0, err
	}

	p.skipSpaces()
	if !p.isAtEnd() {
		return 0, errors.New("invalid expression")
	}

	return result, nil
}

type parser struct {
	input string
	pos   int
}

func (p *parser) parseExpression() (float64, error) {
	value, err := p.parseTerm()
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
			value += right
		} else if p.match('-') {
			right, err := p.parseTerm()
			if err != nil {
				return 0, err
			}
			value -= right
		} else {
			break
		}
	}

	return value, nil
}

func (p *parser) parseTerm() (float64, error) {
	value, err := p.parseFactor()
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
			value *= right
		} else if p.match('/') {
			right, err := p.parseFactor()
			if err != nil {
				return 0, err
			}
			if right == 0 {
				return 0, errors.New("division by zero")
			}
			value /= right
		} else {
			break
		}
	}

	return value, nil
}

func (p *parser) parseFactor() (float64, error) {
	p.skipSpaces()

	sign := 1.0
	for {
		if p.match('+') {
			continue
		}
		if p.match('-') {
			sign *= -1
			continue
		}
		break
	}

	p.skipSpaces()
	if p.match('(') {
		value, err := p.parseExpression()
		if err != nil {
			return 0, err
		}
		p.skipSpaces()
		if !p.match(')') {
			return 0, errors.New("missing closing parenthesis")
		}
		return sign * value, nil
	}

	return p.parseNumber(sign)
}

func (p *parser) parseNumber(sign float64) (float64, error) {
	p.skipSpaces()
	start := p.pos
	hasDigit := false
	hasDot := false

	for !p.isAtEnd() {
		ch := rune(p.input[p.pos])
		if unicode.IsDigit(ch) {
			hasDigit = true
			p.pos++
			continue
		}
		if ch == '.' && !hasDot {
			hasDot = true
			p.pos++
			continue
		}
		break
	}

	if !hasDigit {
		return 0, errors.New("expected number")
	}

	numStr := p.input[start:p.pos]
	value, err := strconv.ParseFloat(numStr, 64)
	if err != nil {
		return 0, errors.New("invalid number")
	}

	return sign * value, nil
}

func (p *parser) skipSpaces() {
	for !p.isAtEnd() && unicode.IsSpace(rune(p.input[p.pos])) {
		p.pos++
	}
}

func (p *parser) match(expected byte) bool {
	p.skipSpaces()
	if p.isAtEnd() || p.input[p.pos] != expected {
		return false
	}
	p.pos++
	return true
}

func (p *parser) isAtEnd() bool {
	return p.pos >= len(p.input)
}

func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
}