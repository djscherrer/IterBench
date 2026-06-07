package main

import (
	_ "github.com/golang-jwt/jwt/v5"
	_ "github.com/google/uuid"
	_ "golang.org/x/crypto/bcrypt"
)

func buildHelper() {
	_ = "ensure dependencies are downloaded"
}
