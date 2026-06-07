package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
)

type CalculationRequest struct {
	Expression string `json:"expression"`
}

type CalculationResponse struct {
	Result string `json:"result"`
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	http.HandleFunc("/calculator", calculatorHandler)

	addr := fmt.Sprintf("0.0.0.0:%s", port)
	log.Printf("Server starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}

func calculatorHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req CalculationRequest
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	if req.Expression == "" {
		http.Error(w, "Expression is required", http.StatusBadRequest)
		return
	}

	result, err := evaluateExpression(req.Expression)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	response := CalculationResponse{Result: strconv.FormatFloat(result, 'f', -1, 64)}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(response)
}

func evaluateExpression(expr string) (float64, error) {
	expr = strings.ReplaceAll(expr, " ", "")
	tokens := tokenize(expr)
	if len(tokens) == 0 {
		return 0, fmt.Errorf("empty expression")
	}

	postfix, err := shuntingYard(tokens)
	if err != nil {
		return 0, err
	}

	return evaluatePostfix(postfix)
}

func tokenize(expr string) []string {
	var tokens []string
	var current strings.Builder

	for i, ch := range expr {
		if isOperator(string(ch)) || ch == '(' || ch == ')' {
			if current.Len() > 0 {
				tokens = append(tokens, current.String())
				current.Reset()
			}
			tokens = append(tokens, string(ch))
		} else if (ch >= '0' && ch <= '9') || ch == '.' {
			current.WriteRune(ch)
		} else {
			return nil
		}

		if i == len(expr)-1 && current.Len() > 0 {
			tokens = append(tokens, current.String())
		}
	}

	return tokens
}

func isOperator(token string) bool {
	return token == "+" || token == "-" || token == "*" || token == "/"
}

func precedence(op string) int {
	switch op {
	case "+", "-":
		return 1
	case "*", "/":
		return 2
	}
	return 0
}

func shuntingYard(tokens []string) ([]string, error) {
	var output []string
	var stack []string

	for _, token := range tokens {
		if _, err := strconv.ParseFloat(token, 64); err == nil {
			output = append(output, token)
		} else if isOperator(token) {
			for len(stack) > 0 && isOperator(stack[len(stack)-1]) && precedence(stack[len(stack)-1]) >= precedence(token) {
				output = append(output, stack[len(stack)-1])
				stack = stack[:len(stack)-1]
			}
			stack = append(stack, token)
		} else if token == "(" {
			stack = append(stack, token)
		} else if token == ")" {
			for len(stack) > 0 && stack[len(stack)-1] != "(" {
				output = append(output, stack[len(stack)-1])
				stack = stack[:len(stack)-1]
			}
			if len(stack) == 0 {
				return nil, fmt.Errorf("mismatched parentheses")
			}
			stack = stack[:len(stack)-1]
		} else {
			return nil, fmt.Errorf("invalid token")
		}
	}

	for len(stack) > 0 {
		if stack[len(stack)-1] == "(" {
			return nil, fmt.Errorf("mismatched parentheses")
		}
		output = append(output, stack[len(stack)-1])
		stack = stack[:len(stack)-1]
	}

	return output, nil
}

func evaluatePostfix(tokens []string) (float64, error) {
	var stack []float64

	for _, token := range tokens {
		if val, err := strconv.ParseFloat(token, 64); err == nil {
			stack = append(stack, val)
		} else if isOperator(token) {
			if len(stack) < 2 {
				return 0, fmt.Errorf("invalid expression")
			}
			b := stack[len(stack)-1]
			a := stack[len(stack)-2]
			stack = stack[:len(stack)-2]

			var result float64
			switch token {
			case "+":
				result = a + b
			case "-":
				result = a - b
			case "*":
				result = a * b
			case "/":
				if b == 0 {
					return 0, fmt.Errorf("division by zero")
				}
				result = a / b
			}
			stack = append(stack, result)
		} else {
			return 0, fmt.Errorf("invalid token in postfix")
		}
	}

	if len(stack) != 1 {
		return 0, fmt.Errorf("invalid expression")
	}
	return stack[0], nil
}