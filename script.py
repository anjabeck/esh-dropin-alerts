import requests
import json
import datetime as dt
import os
import re
from bs4 import BeautifulSoup
import sys


BASE = "https://app.amilia.com/store/en/eshcircusarts"
EVENTS_ENDPOINT = f"{BASE}/api/Organization/EventsForProgram"


PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

API_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = "notified_state.json"

categories = { # Might use later somehow
    "Ground Classes": 6986661,
    "Aerials": 6986650,
    "Practice Time": 6986672,
    "Open Studio": 6986677,
    "Online Classes": 6986670
}


def get_all_program_ids():
    resp = requests.get(f"{BASE}/shop/programs", headers=PAGE_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    programs = []
    for link in soup.select("a.store-program-result__link"):
        href = link.get("href", "")
        match = re.search(r"/shop/programs/(\d+)", href)
        if not match:
            continue
        title_tag = link.select_one(".store-program-result__title")
        programs.append({
            "program_id": match.group(1),
            "name": title_tag.get_text(strip=True) if title_tag else None,
        })
    return programs


def fetch_events(program_id, category_id, subcategory_id, start, end, session):
    params = {
        "programId": program_id,
        "categoryId": category_id,
        "subCategoryId": subcategory_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "_": "0",
    }
    resp = session.get(EVENTS_ENDPOINT, headers=API_HEADERS, params=params)
    resp.raise_for_status()
    return resp.json()

def fetch_range(program_id, category_id, subcategory_id, start, end):
    all_events = []
    session = requests.Session()
    current = start
    while current < end:
        week_end = min(current + dt.timedelta(days=6), end)
        all_events.extend(fetch_events(program_id, category_id, subcategory_id, current, week_end, session))
        current = week_end + dt.timedelta(days=1)
    return all_events

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()

def save_state(notified_ids):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(notified_ids), f)

def check_conditions(conditions, event):
    for label, condition in conditions.items():
        if condition.get("SubCategoryName") and event.get("SubCategoryName") != condition["SubCategoryName"]:
            continue
        if condition.get("ActivityName") and event.get("ActivityName") != condition["ActivityName"]:
            continue
        if condition.get("Level") and not (condition["Level"] in event.get("ActivityName", "")):
            continue
        return label, True
    return None, False

def get_relevant_info(event):
    evtdate = dt.datetime.strptime(event['start'], '%Y-%m-%dT%H:%M:%S.%f')
    weekday = evtdate.strftime('%A')[0:3]
    date_str = evtdate.strftime('%d %b')
    time_str = evtdate.strftime('%I:%M %p').lstrip('0')
    name = event.get('ActivityName', 'Unknown Activity').split(" - ")[1]
    return f"{weekday} {date_str} {time_str} | {name}"

def check_and_notify(conditions):
    start = dt.datetime.today().astimezone()
    end = start + dt.timedelta(weeks=10)
    programs = get_all_program_ids()

    notified = load_state()
    updated = False

    currently_open = set()

    message = []

    for p in programs:
        events = fetch_range(p["program_id"], None, None, start, end)

        for e in events:
            label, matches = check_conditions(conditions, e)
            if matches and e.get("HasPlaceLeft", False):
                segment_id = str(e.get("SegmentId"))
                currently_open.add(segment_id)
                if segment_id not in notified:
                    event_info = get_relevant_info(e)
                    message.append(f"{label} | {event_info}")
                notified.add(segment_id)
                updated = True
    notify_message = "\n".join(sorted(message))
    if notify_message:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                      data=notify_message.encode("utf-8"),
                      headers={"Title": "New spots opened!"})

    # Clean up: if a spot filled back up again, remove it from "notified"
    # so you get re-notified if it opens up again later
    stale = notified - currently_open
    if stale:
        notified -= stale
        updated = True

    if updated:
        save_state(notified)

def send_summary(conditions, frequency):
    start = dt.datetime.today().astimezone()
    if frequency == "daily":
        end = start + dt.timedelta(days=1)
    else:
        end = start + dt.timedelta(days=7)
    programs = get_all_program_ids()
    lines = []
    for p in programs:
        events = fetch_range(p["program_id"], None, None, start, end)
        for e in events:
            label, matches = check_conditions(conditions, e)
            if matches and e.get("HasPlaceLeft", False):
                event_info = get_relevant_info(e)
                lines.append(f"{label} | {event_info}")

    if frequency == "daily":
        freq = "tomorrow"
    else:
        freq = "this week"
    if not lines:
        summary = f"No classes with open spots {freq}."
    else:
        summary = "\n".join(sorted(lines))

    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=summary.encode("utf-8"),
        headers={"Title": f"Available classes {freq}"},
    )


if __name__ == "__main__":

    assert NTFY_TOPIC, "NTFY_TOPIC environment variable must be set"
    assert os.path.exists("conditions.json"), "conditions.json file must exist"
    assert sys.argv[1] in ("weekly","daily") if len(sys.argv) > 1 else True, "Invalid argument. Use 'weekly' or 'daily' for summary."

    # Load conditions
    with open("conditions.json") as f:
        conditions = json.load(f)

    if len(sys.argv) > 1:
        send_summary(conditions, sys.argv[1])
    else:
        check_and_notify(conditions)


