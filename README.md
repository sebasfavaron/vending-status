# vending-status

Simple GitHub Pages dashboard for OurVend machine status.

## Secrets

Set these repo secrets:

- `OURVEND_USERNAME`
- `OURVEND_PASSWORD`

## What it does

- fetches status from OurVend PC + H5 web apps
- builds `status.json`
- deploys a static dashboard to GitHub Pages
- runs on schedule and manually
