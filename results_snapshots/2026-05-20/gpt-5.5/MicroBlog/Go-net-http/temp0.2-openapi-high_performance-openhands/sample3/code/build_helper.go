//go:build ignore

package main

import (
	"fmt"
	_ "github.com/golang-jwt/jwt/v5"
	_ "github.com/google/uuid"
	_ "github.com/lib/pq"
	_ "golang.org/x/crypto/bcrypt"
)

func main() {
	fmt.Println("build helper")
}
