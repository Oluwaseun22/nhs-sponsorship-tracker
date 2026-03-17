# NHS Sponsorship Job Tracker

An automated pipeline that scans NHS Jobs UK every 6 hours for data/analytics roles offering visa sponsorship, generates a tailored CV, and sends an email alert.

## Architecture

EventBridge (every 6h) → AWS Lambda → S3 (job data) + SES (email alerts)
                                ↕
                          DynamoDB (deduplication)

## Features

- Scrapes NHS Jobs UK API for data, analytics, and engineering roles
- Filters jobs mentioning visa sponsorship / Certificate of Sponsorship
- Auto-generates a tailored CV (python-docx) for each matched role
- Sends email alerts via AWS SES with CV attached
- Deduplicates using DynamoDB — no repeat alerts
- Stores all scan data in S3
- Live dashboard hosted on S3 static website

## Tech Stack

Python · AWS Lambda · DynamoDB · S3 · SES · EventBridge · python-docx

## Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your values
3. Run `cd infra && ./deploy.sh` to deploy everything to AWS

## Dashboard

Live at: https://d12tf1qc9p6i82.cloudfront.net/nhs_dashboard.html

## Author

Segun Toriola — [LinkedIn](https://linkedin.com/in/oluwasegun)
