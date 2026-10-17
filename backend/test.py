import requests

from config import settings

EDGAR_USER_AGENT = settings.EDGAR_USER_AGENT

HEADERS = {
    "User-Agent": EDGAR_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov"
}

url = "https://data.sec.gov/files/company_tickers.json"

response = requests.get(url, headers=HEADERS)

print(response.status_code)