package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"os"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/lrprojects/monaserver/internal/config"
	"github.com/lrprojects/monaserver/internal/db"
)

func main() {
	slug := flag.String("slug", "", "isolated preview slug")
	username := flag.String("username", "", "fixture username")
	flag.Parse()

	if *slug == "" || *username == "" {
		fail("preview slug and fixture username are required")
	}
	cfg, err := config.Load()
	if err != nil {
		fail("could not load preview database configuration")
	}
	parsed, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil || !isLoopback(parsed.ConnConfig.Host) || parsed.ConnConfig.Database != "ms_preview_"+strings.ReplaceAll(*slug, "-", "_") {
		fail("refusing to update an account outside this worktree's preview database")
	}

	ctx := context.Background()
	pool, err := db.NewPool(ctx, cfg.DatabaseURL)
	if err != nil {
		fail("could not connect to the preview database")
	}
	defer pool.Close()
	queries := db.New(pool)
	user, err := queries.GetUserByUsername(ctx, *username)
	if err != nil || user == nil || user.Email == nil || !strings.HasSuffix(strings.ToLower(*user.Email), "@example.invalid") {
		fail("preview fixture email is missing or unexpected")
	}
	confirmed, err := queries.ConfirmUserEmailWithClaim(ctx, user.ID)
	if err != nil || !confirmed {
		fail("could not enable email sign-in for the preview fixture")
	}
	fmt.Println("Preview fixture email is ready.")
}

func isLoopback(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func fail(message string) {
	fmt.Fprintln(os.Stderr, message)
	os.Exit(1)
}
