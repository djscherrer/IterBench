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

	fmt.Printf("Server listening on 0.0.0.0:%s\n", port)
	if err := http.ListenAndServe("0.0.0.0:"+port, nil); err != nil {
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

func tokenize(expr string) ([]token, error) {
	var tokens []token
	i := 0
	runes := []rune(strings.TrimSpace(expr))

	for i < len(runes) {
		ch := runes[i]

		if unicode.IsSpace(ch) {
			i++
			continue
		}

		if ch == '+' {
			tokens = append(tokens, token{typ: tokenPlus})
			i++
		} else if ch == '-' {
			tokens = append(tokens, token{typ: tokenMinus})
			i++
		} else if ch == '*' {
			tokens = append(tokens, token{typ: tokenMul})
			i++
		} else if ch == '/' {
			tokens = append(tokens, token{typ: tokenDiv})
			i++
		} else if ch == '(' {
			tokens = append(tokens, token{typ: tokenLParen})
			i++
		} else if ch == ')' {
			tokens = append(tokens, token{typ: tokenRParen})
			i++
		} else if unicode.IsDigit(ch) || ch == '.' {
			j := i
			for j < len(runes) && (unicode.IsDigit(runes[j]) || runes[j] == '.') {
				j++
			}
			numStr := string(runes[i:j])
			val, err := strconv.ParseFloat(numStr, 64)
			if err != nil {
				return nil, fmt.Errorf("invalid number: %s", numStr)
			}
			tokens = append(tokens, token{typ: tokenNumber, val: val})
			i = j
		} else {
			return nil, fmt.Errorf("unexpected character: %c", ch)
		}
	}

	tokens = append(tokens, token{typ: tokenEOF})
	return tokens, nil
}

type parser struct {
	tokens []token
	pos    int
}

func (p *parser) peek() token {
	if p.pos < len(p.tokens) {
		return p.tokens[p.pos]
	}
	return token{typ: tokenEOF}
}

func (p *parser) advance() token {
	t := p.peek()
	p.pos++
	return t
}

func (p *parser) parseExpression() (float64, error) {
	return p.parseAddSub()
}

func (p *parser) parseAddSub() (float64, error) {
	left, err := p.parseMulDiv()
	if err != nil {
		return 0, err
	}

	for {
		t := p.peek()
		if t.typ == tokenPlus {
			p.advance()
			right, err := p.parseMulDiv()
			if err != nil {
				return 0, err
			}
			left += right
		} else if t.typ == tokenMinus {
			p.advance()
			right, err := p.parseMulDiv()
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

func (p *parser) parseMulDiv() (float64, error) {
	left, err := p.parseUnary()
	if err != nil {
		return 0, err
	}

	for {
		t := p.peek()
		if t.typ == tokenMul {
			p.advance()
			right, err := p.parseUnary()
			if err != nil {
				return 0, err
			}
			left *= right
		} else if t.typ == tokenDiv {
			p.advance()
			right, err := p.parseUnary()
			if err != nil {
				return 0, err
			}
			if right == 0 {
				return 0, fmt.Errorf("division by zero")
			}
			left /= right
		} else {
			break
		}
	}

	return left, nil
}

func (p *parser) parseUnary() (float64, error) {
	t := p.peek()
	if t.typ == tokenMinus {
		p.advance()
		val, err := p.parseUnary()
		if err != nil {
			return 0, err
		}
		return -val, nil
	}
	if t.typ == tokenPlus {
		p.advance()
		return p.parseUnary()
	}
	return p.parsePrimary()
}

func (p *parser) parsePrimary() (float64, error) {
	t := p.peek()

	if t.typ == tokenNumber {
		p.advance()
		return t.val, nil
	}

	if t.typ == tokenLParen {
		p.advance()
		val, err := p.parseExpression()
		if err != nil {
			return 0, err
		}
		if p.peek().typ != tokenRParen {
			return 0, fmt.Errorf("expected closing parenthesis")
		}
		p.advance()
		return val, nil
	}

	return 0, fmt.Errorf("unexpected token")
}

func evaluate(expr string) (float64, error) {
	tokens, err := tokenize(expr)
	if err != nil {
		return 0, err
	}

	p := &parser{tokens: tokens}
	result, err := p.parseExpression()
	if err != nil {
		return 0, err
	}

	if p.peek().typ != tokenEOF {
		return 0, fmt.Errorf("unexpected tokens after expression")
	}

	return result, nil
}