package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"unicode"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	http.HandleFunc("/calculator", handleCalculator)

	addr := "0.0.0.0:" + port
	fmt.Printf("Server listening on %s\n", addr)
	if err := http.ListenAndServe(addr, nil); err != nil {
		fmt.Fprintf(os.Stderr, "Server error: %v\n", err)
		os.Exit(1)
	}
}

func handleCalculator(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Expression string `json:"expression"`
	}

	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	result, err := evaluate(req.Expression)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	// Format result nicely
	resultStr := formatNumber(result)

	w.Header().Set("Content-Type", "application/json")
	resp := map[string]string{"result": resultStr}
	json.NewEncoder(w).Encode(resp)
}

func formatNumber(f float64) string {
	if f == float64(int64(f)) {
		return strconv.FormatInt(int64(f), 10)
	}
	return strconv.FormatFloat(f, 'f', -1, 64)
}

// Tokenizer and parser for arithmetic expressions
// Supports +, -, *, /, parentheses, and unary minus

type tokenType int

const (
	tokenNumber tokenType = iota
	tokenPlus
	tokenMinus
	tokenMul
	tokenDiv
	tokenLParen
	tokenRParen
	tokenEOF
)

type token struct {
	typ tokenType
	val float64
}

type lexer struct {
	input string
	pos   int
}

func newLexer(input string) *lexer {
	return &lexer{input: input, pos: 0}
}

func (l *lexer) skipWhitespace() {
	for l.pos < len(l.input) && unicode.IsSpace(rune(l.input[l.pos])) {
		l.pos++
	}
}

func (l *lexer) nextToken() (token, error) {
	l.skipWhitespace()
	if l.pos >= len(l.input) {
		return token{typ: tokenEOF}, nil
	}

	ch := l.input[l.pos]

	switch ch {
	case '+':
		l.pos++
		return token{typ: tokenPlus}, nil
	case '-':
		l.pos++
		return token{typ: tokenMinus}, nil
	case '*':
		l.pos++
		return token{typ: tokenMul}, nil
	case '/':
		l.pos++
		return token{typ: tokenDiv}, nil
	case '(':
		l.pos++
		return token{typ: tokenLParen}, nil
	case ')':
		l.pos++
		return token{typ: tokenRParen}, nil
	default:
		if ch == '.' || (ch >= '0' && ch <= '9') {
			start := l.pos
			for l.pos < len(l.input) && ((l.input[l.pos] >= '0' && l.input[l.pos] <= '9') || l.input[l.pos] == '.') {
				l.pos++
			}
			numStr := l.input[start:l.pos]
			val, err := strconv.ParseFloat(numStr, 64)
			if err != nil {
				return token{}, fmt.Errorf("invalid number: %s", numStr)
			}
			return token{typ: tokenNumber, val: val}, nil
		}
		return token{}, fmt.Errorf("unexpected character: %c", ch)
	}
}

type parser struct {
	lexer   *lexer
	current token
}

func newParser(input string) (*parser, error) {
	l := newLexer(input)
	t, err := l.nextToken()
	if err != nil {
		return nil, err
	}
	return &parser{lexer: l, current: t}, nil
}

func (p *parser) advance() error {
	t, err := p.lexer.nextToken()
	if err != nil {
		return err
	}
	p.current = t
	return nil
}

func (p *parser) parseExpression() (float64, error) {
	return p.parseAddSub()
}

func (p *parser) parseAddSub() (float64, error) {
	left, err := p.parseMulDiv()
	if err != nil {
		return 0, err
	}

	for p.current.typ == tokenPlus || p.current.typ == tokenMinus {
		op := p.current.typ
		if err := p.advance(); err != nil {
			return 0, err
		}
		right, err := p.parseMulDiv()
		if err != nil {
			return 0, err
		}
		if op == tokenPlus {
			left += right
		} else {
			left -= right
		}
	}
	return left, nil
}

func (p *parser) parseMulDiv() (float64, error) {
	left, err := p.parseUnary()
	if err != nil {
		return 0, err
	}

	for p.current.typ == tokenMul || p.current.typ == tokenDiv {
		op := p.current.typ
		if err := p.advance(); err != nil {
			return 0, err
		}
		right, err := p.parseUnary()
		if err != nil {
			return 0, err
		}
		if op == tokenMul {
			left *= right
		} else {
			if right == 0 {
				return 0, fmt.Errorf("division by zero")
			}
			left /= right
		}
	}
	return left, nil
}

func (p *parser) parseUnary() (float64, error) {
	if p.current.typ == tokenMinus {
		if err := p.advance(); err != nil {
			return 0, err
		}
		val, err := p.parseUnary()
		if err != nil {
			return 0, err
		}
		return -val, nil
	}
	if p.current.typ == tokenPlus {
		if err := p.advance(); err != nil {
			return 0, err
		}
		return p.parseUnary()
	}
	return p.parsePrimary()
}

func (p *parser) parsePrimary() (float64, error) {
	if p.current.typ == tokenNumber {
		val := p.current.val
		if err := p.advance(); err != nil {
			return 0, err
		}
		return val, nil
	}
	if p.current.typ == tokenLParen {
		if err := p.advance(); err != nil {
			return 0, err
		}
		val, err := p.parseExpression()
		if err != nil {
			return 0, err
		}
		if p.current.typ != tokenRParen {
			return 0, fmt.Errorf("expected closing parenthesis")
		}
		if err := p.advance(); err != nil {
			return 0, err
		}
		return val, nil
	}
	return 0, fmt.Errorf("unexpected token")
}

func evaluate(expression string) (float64, error) {
	expression = strings.TrimSpace(expression)
	if expression == "" {
		return 0, fmt.Errorf("empty expression")
	}

	p, err := newParser(expression)
	if err != nil {
		return 0, err
	}

	result, err := p.parseExpression()
	if err != nil {
		return 0, err
	}

	if p.current.typ != tokenEOF {
		return 0, fmt.Errorf("unexpected token at end of expression")
	}

	return result, nil
}