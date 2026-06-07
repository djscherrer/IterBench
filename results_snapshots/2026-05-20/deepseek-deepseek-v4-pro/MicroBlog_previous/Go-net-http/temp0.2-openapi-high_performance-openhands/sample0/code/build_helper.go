package main

import (
	"fmt"
	_ "github.com/lib/pq"
	_ "github.com/golang-jwt/jwt/v5"
	_ "github.com/google/uuid"
	_ "golang.org/x/crypto/bcrypt"
)

func init() {
	fmt.Println("build helper")
}
