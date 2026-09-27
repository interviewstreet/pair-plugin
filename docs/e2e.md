# End-to-end review opener

`scripts/open_review.py` opens one review against a pair-be instance that is
already running locally:

```text
python scripts/open_review.py <review_key> [--base-url http://127.0.0.1:8080] [--timeout 600] [--interval 5]
```

The script reads the signing key from
`PAIR_INTERNAL_AUTH_JWT_SIGNING_KEY`. pair-be must already be running locally
with the same key. A real review also requires the VPN connection and the
review service used by pair-be.

The script does not create Google Cloud resources.
