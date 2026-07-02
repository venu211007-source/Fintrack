# FinTrack — Personal Finance Dashboard

A self-hosted web app that turns your bank statements into clear financial insights. Upload a PDF, CSV, or Excel statement and instantly see your income, expenses, spending categories, and net worth — all in one place.

## Features

- **Multi-bank statement upload** — PDF, CSV, and Excel supported
- **Auto-categorization** — transactions tagged automatically (Food, Shopping, EMI, Investment, etc.)
- **Dashboard** — monthly income vs expense chart, top spending categories, recent transactions
- **Reports** — month-by-month breakdown across the year
- **Transactions** — full searchable list with date range filters (last 7/15/30 days or custom)
- **Net Worth** — tracks assets and liabilities over time
- **Multi-currency** — live exchange rates via European Central Bank
- **Multi-user** — each user has their own private account and data

## Supported Banks

| Region | Banks |
|---|---|
| India | HDFC, SBI, ICICI, Axis, Kotak, Yes Bank, IndusInd, Federal, IDFC First, IDBI |
| Europe | Deutsche Bank, Barclays, HSBC, Santander, BNP Paribas, ING |
| USA | Chase, Bank of America, Wells Fargo, Citibank, Capital One, Amex |

## Tech Stack

- **Backend** — Python, Flask, Flask-SQLAlchemy, Flask-Login
- **Database** — PostgreSQL (production) / SQLite (local)
- **Frontend** — Bootstrap 5, Chart.js
- **PDF Parsing** — pdfplumber
- **Exchange Rates** — Frankfurter API (free, no API key needed)
- **Hosting** — Railway

## Running Locally

git clone https://github.com/venu211007-source/Fintrack.git
cd Fintrack
pip install -r requirements.txt
python app.py

Open http://localhost:5000, register an account, add a bank account, upload your statement.

Live Demo
https://fintrack-production-f457.up.railway.app

Data Privacy
All data stays in your own database. No transaction data is sent to any third party.