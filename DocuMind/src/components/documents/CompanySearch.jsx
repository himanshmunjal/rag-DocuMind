// Search a company, list its filings, ingest one.

import { useState, useEffect, useRef, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";

const FORM_TYPES = ["10-K", "10-Q", "8-K"];

export default function CompanySearch({ onIngestComplete }) {
  const [query, setQuery] = useState("");
  const [companies, setCompanies] = useState([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState(null);

  const [selectedCompany, setSelectedCompany] = useState(null);
  const [formType, setFormType] = useState("10-K");
  const [filings, setFilings] = useState([]);
  const [filingsLoading, setFilingsLoading] = useState(false);
  const [filingsError, setFilingsError] = useState(null);

  const [ingestingAccession, setIngestingAccession] = useState(null);
  const [ingestedAccessions, setIngestedAccessions] = useState(new Set());
  const [ingestError, setIngestError] = useState(null);

  const debounceRef = useRef(null);

  // --- company search (debounced) ---
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);

    if (!query.trim()) {
      setCompanies([]);
      setSearchError(null);
      return;
    }

    debounceRef.current = setTimeout(async () => {
      setSearching(true);
      setSearchError(null);
      try {
        const res = await fetch(
          `${API_BASE}/companies/search?q=${encodeURIComponent(query.trim())}`
        );
        if (!res.ok) throw new Error(`Search failed (${res.status})`);
        const data = await res.json();
        // /companies/search returns { query, results: [...] }
        setCompanies(data.results ?? []);
      } catch (err) {
        setSearchError(err.message || "Couldn't search companies right now.");
        setCompanies([]);
      } finally {
        setSearching(false);
      }
    }, 300);

    return () => clearTimeout(debounceRef.current);
  }, [query]);

  // --- filings list for selected company + form type ---
  const loadFilings = useCallback(async (company, type) => {
    setFilingsLoading(true);
    setFilingsError(null);
    setFilings([]);
    try {
      const res = await fetch(
        `${API_BASE}/companies/${company.cik}/filings?form_type=${encodeURIComponent(type)}`
      );
      if (!res.ok) throw new Error(`Couldn't load filings (${res.status})`);
      const data = await res.json();
      // /companies/{cik}/filings returns { cik, company_name, filings: [...] }
      setFilings(data.filings ?? []);
    } catch (err) {
      setFilingsError(err.message || "Couldn't load filings right now.");
    } finally {
      setFilingsLoading(false);
    }
  }, []);

  const handleSelectCompany = (company) => {
    setSelectedCompany(company);
    setCompanies([]);
    setQuery(`${company.name} (${company.ticker})`);
    loadFilings(company, formType);
  };

  const handleFormTypeChange = (type) => {
    setFormType(type);
    if (selectedCompany) loadFilings(selectedCompany, type);
  };

  const handleIngest = async (filing) => {
    setIngestingAccession(filing.accession_number);
    setIngestError(null);
    try {
      const res = await fetch(`${API_BASE}/filings/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cik: filing.cik,
          accession_number: filing.accession_number,
          form_type: filing.form_type,
          primary_document: filing.primary_document,
        }),
      });
      if (!res.ok) throw new Error(`Ingest failed (${res.status})`);
      const data = await res.json();

      setIngestedAccessions((prev) => new Set(prev).add(filing.accession_number));
      onIngestComplete?.(data);
    } catch (err) {
      setIngestError(err.message || "Ingest failed. Try again.");
    } finally {
      setIngestingAccession(null);
    }
  };

  const handleReset = () => {
    setSelectedCompany(null);
    setQuery("");
    setFilings([]);
    setFilingsError(null);
  };

  return (
    <div className="w-full flex flex-col gap-3 font-['DM_Mono',monospace]">
      {/* search input */}
      <div className="relative">
        <input
          type="text"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            if (selectedCompany) setSelectedCompany(null);
          }}
          placeholder="Search a company — e.g. Google, AAPL"
          className="w-full rounded-lg border border-[#242424] bg-[#111] px-3.5 py-2.5 text-xs
                     text-[#e2d5c3] placeholder-[#444] outline-none
                     focus:ring-1 focus:ring-[#d4a853] focus:border-[#d4a853]"
        />
        {searching && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-[10px] text-[#444]">
            searching…
          </span>
        )}

        {/* company dropdown */}
        {companies.length > 0 && (
          <ul className="absolute z-10 mt-1 w-full rounded-lg border border-[#1e1e1e] bg-[#0d0d0d]
                         shadow-lg max-h-64 overflow-y-auto">
            {companies.map((c) => (
              <li key={c.cik}>
                <button
                  type="button"
                  onClick={() => handleSelectCompany(c)}
                  className="w-full text-left px-3.5 py-2 text-xs text-[#e2d5c3]
                             hover:bg-[#161310] flex justify-between items-center gap-2"
                >
                  <span className="truncate">{c.name}</span>
                  <span className="text-[#d4a853] flex-shrink-0">{c.ticker}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {searchError && <p className="text-xs text-[#f87171]">{searchError}</p>}

      {/* selected company + filings */}
      {selectedCompany && (
        <div className="rounded-lg border border-[#1a1a1a] bg-[#0d0d0d] p-3.5 flex flex-col gap-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-xs text-[#e2d5c3] truncate font-['Lora',Georgia,serif]">
                {selectedCompany.name}
              </p>
              <p className="text-[10px] text-[#444] mt-0.5">
                {selectedCompany.ticker} · CIK {selectedCompany.cik}
              </p>
            </div>
            <button
              type="button"
              onClick={handleReset}
              className="text-[10px] text-[#444] hover:text-[#d4a853] flex-shrink-0"
            >
              change
            </button>
          </div>

          <div className="flex gap-1.5">
            {FORM_TYPES.map((type) => (
              <button
                key={type}
                type="button"
                onClick={() => handleFormTypeChange(type)}
                className={`px-2.5 py-1 rounded-full text-[10px] font-medium border transition-colors ${
                  formType === type
                    ? "bg-[#d4a853] text-[#0a0a0a] border-[#d4a853]"
                    : "bg-transparent text-[#888] border-[#242424] hover:border-[#d4a853] hover:text-[#d4a853]"
                }`}
              >
                {type}
              </button>
            ))}
          </div>

          {filingsLoading && <p className="text-xs text-[#444]">Loading filings…</p>}
          {filingsError && <p className="text-xs text-[#f87171]">{filingsError}</p>}

          {!filingsLoading && !filingsError && filings.length === 0 && (
            <p className="text-xs text-[#444]">No {formType} filings found.</p>
          )}

          <ul className="flex flex-col gap-1.5">
            {filings.map((f) => {
              const isIngested = ingestedAccessions.has(f.accession_number) || f.ingested;
              const isIngesting = ingestingAccession === f.accession_number;
              return (
                <li
                  key={f.accession_number}
                  className="flex items-center justify-between rounded-md border border-[#1a1a1a]
                             bg-[#0a0a0a] px-3 py-2 text-xs gap-2"
                >
                  <div className="min-w-0">
                    <p className="text-[#e2d5c3] truncate">
                      {f.form_type} — filed {f.filing_date}
                    </p>
                    <p className="text-[10px] text-[#444] mt-0.5">
                      Period: {f.report_date || "—"}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={isIngesting || isIngested}
                    onClick={() => handleIngest(f)}
                    className={`px-2.5 py-1.5 rounded-md text-[10px] font-medium flex-shrink-0 transition-colors ${
                      isIngested
                        ? "bg-[#4ade8014] text-[#4ade80] cursor-default"
                        : "bg-[#d4a853] text-[#0a0a0a] hover:bg-[#e0b666] disabled:opacity-50"
                    }`}
                  >
                    {isIngested ? "Ingested ✓" : isIngesting ? "Ingesting…" : "Ingest"}
                  </button>
                </li>
              );
            })}
          </ul>

          {ingestError && <p className="text-xs text-[#f87171]">{ingestError}</p>}
        </div>
      )}
    </div>
  );
}