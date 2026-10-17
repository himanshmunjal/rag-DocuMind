import requests

EDGAR_USER_AGENT = (
    "DocuMind/1.0 (Research tool; your-email@example.com)"
)

HEADERS = {
    "User-Agent": EDGAR_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov"
}

url = "https://data.sec.gov/files/company_tickers.json"

response = requests.get(url, headers=HEADERS)

print(response.status_code)