#!/usr/bin/env python3
"""
Google Job Alerts RSS Feed Fetcher
===================================
Fetches job listings from Google Alerts RSS feeds,
parses them, filters out staffing/consulting companies,
and outputs structured JSON for the dashboard.
"""

import json
import os
import re
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# Paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DATA_DIR = PROJECT_DIR / "data"
JOBS_FILE = DATA_DIR / "jobs.json"


def load_config():
    """Load configuration from config.yaml."""
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def generate_job_id(title, link, company):
    """Generate a unique ID for a job listing."""
    raw = f"{title}|{link}|{company}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def extract_company_from_url(url):
    """Try to extract company name from the job URL."""
    patterns = [
        # Workday: company.wd5.myworkdayjobs.com
        r"(\w+)\.wd\d+\.myworkdayjobs\.com",
        # Greenhouse: boards.greenhouse.io/company
        r"boards\.greenhouse\.io/(\w+)",
        # Lever: jobs.lever.co/company
        r"jobs\.lever\.co/([\w-]+)",
        # SmartRecruiters: jobs.smartrecruiters.com/Company
        r"jobs\.smartrecruiters\.com/([\w-]+)",
        # Ashby: jobs.ashbyhq.com/company
        r"jobs\.ashbyhq\.com/([\w-]+)",
        # BambooHR: company.bamboohr.com
        r"([\w-]+)\.bamboohr\.com",
        # Breezy: company.breezy.hr
        r"([\w-]+)\.breezy\.hr",
        # Jobvite: jobs.jobvite.com/company
        r"jobs\.jobvite\.com/([\w-]+)",
        # Recruitee: company.recruitee.com
        r"([\w-]+)\.recruitee\.com",
        # Workable: apply.workable.com/company
        r"apply\.workable\.com/([\w-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            name = match.group(1)
            # Clean up: replace hyphens with spaces, title case
            return name.replace("-", " ").replace("_", " ").title()
    return None


def extract_company_from_title(title):
    """Try to extract company name from the alert title."""
    # Common patterns: "Role at Company", "Role - Company", "Company - Role"
    patterns = [
        r"(?:at|@)\s+(.+?)(?:\s*[-|]|$)",
        r"^(.+?)\s*[-|]\s*.+(?:Engineer|Developer|Manager|Designer|Analyst|Scientist|Lead|Architect)",
        r"(?:Engineer|Developer|Manager|Designer|Analyst|Scientist|Lead|Architect).+?[-|]\s*(.+?)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            company = match.group(1).strip()
            # Remove common suffixes
            company = re.sub(r"\s*\(.*?\)\s*$", "", company)
            if len(company) > 2 and len(company) < 80:
                return company
    return None


def extract_role_from_title(title, known_roles):
    """Extract the job role from the title."""
    title_lower = title.lower()
    for role in known_roles:
        if role.lower() in title_lower:
            return role
    # Fallback: try common patterns
    role_patterns = [
        r"((?:Senior|Junior|Staff|Lead|Principal|Sr\.?|Jr\.?)?\s*\w+\s+(?:Engineer|Developer|Manager|Designer|Analyst|Scientist|Architect|Lead))",
    ]
    for pattern in role_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return title[:60] if len(title) > 60 else title


def extract_region(title, description):
    """Determine the region from the job listing."""
    text = f"{title} {description}".lower()
    regions = []
    if any(kw in text for kw in ["india", "bangalore", "bengaluru", "mumbai", "delhi",
                                   "hyderabad", "pune", "chennai", "kolkata", "noida",
                                   "gurgaon", "gurugram", "ahmedabad", "jaipur"]):
        regions.append("India")
    if any(kw in text for kw in ["philippines", "manila", "cebu", "davao", "makati",
                                   "quezon", "taguig", "pasig", "bgc", "clark",
                                   "onlinejobs.ph", "remotework.ph", "virtualstaff.ph",
                                   "jobstreet.com.ph", "kalibrr"]):
        regions.append("Philippines")
    if any(kw in text for kw in ["remote", "work from home", "wfh", "anywhere",
                                   "distributed", "fully remote"]):
        regions.append("Remote")
    return regions if regions else ["Unknown"]


def is_excluded_company(company_name, excluded_list):
    """Check if a company should be excluded."""
    if not company_name:
        return False
    company_lower = company_name.lower().strip()
    for excluded in excluded_list:
        if excluded.lower() in company_lower or company_lower in excluded.lower():
            return True
    return False


def extract_source_site(url):
    """Extract the job board source from URL."""
    site_names = {
        "myworkdayjobs.com": "Workday",
        "greenhouse.io": "Greenhouse",
        "icims.com": "iCIMS",
        "taleo.net": "Taleo",
        "lever.co": "Lever",
        "smartrecruiters.com": "SmartRecruiters",
        "jobvite.com": "Jobvite",
        "adp.com": "ADP",
        "successfactors.com": "SuccessFactors",
        "brassring.com": "BrassRing",
        "jazzhr.com": "JazzHR",
        "breezy.hr": "Breezy",
        "ashbyhq.com": "Ashby",
        "bamboohr.com": "BambooHR",
        "recruitee.com": "Recruitee",
        "workable.com": "Workable",
        "weworkremotely.com": "WeWorkRemotely",
        "remoteok.com": "RemoteOK",
        "wellfound.com": "Wellfound",
        "naukri.com": "Naukri",
        "foundit.in": "Foundit",
        "instahyre.com": "Instahyre",
        "cutshort.io": "Cutshort",
        "onlinejobs.ph": "OnlineJobs.ph",
        "jobstreet.com": "JobStreet",
        "kalibrr.com": "Kalibrr",
        "linkedin.com": "LinkedIn",
        "indeed.com": "Indeed",
    }
    url_lower = url.lower()
    for domain, name in site_names.items():
        if domain in url_lower:
            return name
    return "Other"


def parse_feed_entry(entry, config):
    """Parse a single RSS feed entry into a job listing."""
    title = entry.get("title", "Unknown")
    link = entry.get("link", "")
    summary = entry.get("summary", "")
    published = entry.get("published", "")

    # Clean HTML from summary
    if summary:
        soup = BeautifulSoup(summary, "html.parser")
        description = soup.get_text(separator=" ", strip=True)
    else:
        description = ""

    # Parse date
    try:
        if published:
            date_obj = date_parser.parse(published)
        else:
            date_obj = datetime.now(timezone.utc)
    except (ValueError, TypeError):
        date_obj = datetime.now(timezone.utc)

    # Extract company
    company = extract_company_from_url(link)
    if not company:
        company = extract_company_from_title(title)
    if not company:
        company = "Unknown Company"

    # Check exclusion
    if is_excluded_company(company, config.get("excluded_companies", [])):
        return None

    # Extract role
    role = extract_role_from_title(title, config.get("roles", []))

    # Extract region
    regions = extract_region(title, description)

    # Extract source
    source = extract_source_site(link)

    job_id = generate_job_id(title, link, company)

    return {
        "id": job_id,
        "title": title,
        "company": company,
        "role": role,
        "regions": regions,
        "link": link,
        "source": source,
        "description": description[:300] + "..." if len(description) > 300 else description,
        "date": date_obj.strftime("%Y-%m-%d"),
        "timestamp": date_obj.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_rss_feeds(config):
    """Fetch and parse all configured RSS feeds."""
    jobs = []
    feeds = config.get("rss_feeds", [])

    for feed_config in feeds:
        url = feed_config.get("url", "")
        name = feed_config.get("name", "Unknown Feed")

        if not url:
            print(f"  Skipping '{name}' - no URL configured")
            continue

        print(f"  Fetching: {name}")
        try:
            response = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (compatible; JobAlertsDashboard/1.0)"
            })
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
                job = parse_feed_entry(entry, config)
                if job:
                    jobs.append(job)

            print(f"    Found {len(feed.entries)} entries, {len(jobs)} after filtering")

        except requests.RequestException as e:
            print(f"    Error fetching {name}: {e}")
        except Exception as e:
            print(f"    Error parsing {name}: {e}")

    return jobs


def load_existing_jobs():
    """Load existing jobs from the JSON file."""
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE, "r") as f:
                data = json.load(f)
                return data.get("jobs", [])
        except (json.JSONDecodeError, KeyError):
            return []
    return []


def merge_jobs(existing, new_jobs):
    """Merge new jobs with existing ones, avoiding duplicates."""
    existing_ids = {job["id"] for job in existing}
    merged = list(existing)

    added = 0
    for job in new_jobs:
        if job["id"] not in existing_ids:
            merged.append(job)
            existing_ids.add(job["id"])
            added += 1

    # Sort by date, newest first
    merged.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # Keep only last 90 days of jobs
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # For now keep all, can add date filtering later

    print(f"  Added {added} new jobs, total: {len(merged)}")
    return merged


def save_jobs(jobs):
    """Save jobs to JSON file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Compute stats
    companies = set()
    roles = set()
    regions_count = {"India": 0, "Philippines": 0, "Remote": 0, "Unknown": 0}
    sources = {}
    dates = set()

    for job in jobs:
        companies.add(job["company"])
        roles.add(job["role"])
        dates.add(job["date"])
        source = job.get("source", "Other")
        sources[source] = sources.get(source, 0) + 1
        for region in job.get("regions", []):
            if region in regions_count:
                regions_count[region] += 1

    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_jobs": len(jobs),
        "stats": {
            "total_companies": len(companies),
            "total_roles": len(roles),
            "regions": regions_count,
            "sources": dict(sorted(sources.items(), key=lambda x: x[1], reverse=True)),
            "date_range": {
                "earliest": min(dates) if dates else None,
                "latest": max(dates) if dates else None,
            }
        },
        "jobs": jobs,
    }

    with open(JOBS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"\nSaved {len(jobs)} jobs to {JOBS_FILE}")


def main():
    print("=" * 50)
    print("Google Job Alerts Dashboard - Feed Fetcher")
    print("=" * 50)

    print("\nLoading configuration...")
    config = load_config()

    print("\nFetching RSS feeds...")
    new_jobs = fetch_rss_feeds(config)

    print("\nMerging with existing data...")
    existing_jobs = load_existing_jobs()
    all_jobs = merge_jobs(existing_jobs, new_jobs)

    print("\nSaving results...")
    save_jobs(all_jobs)

    print("\nDone!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
