"""Sleuth incident service: case API + private workspace website + monitoring worker. Separate from the CLI; imports
sleuth.* as a read-only library. Payment code lives only in service/payments and is never imported by collectors."""
SERVICE_VERSION = "service/0.1.0"
