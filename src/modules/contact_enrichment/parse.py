"""Read SearchBug api_ppl responses into plain person records.

Live responses (TYPE=api_ppl, FORMAT=JSON) look like:
  {"rows": "2", "people": {"person": [ {
      "reportToken", "deceased": "Yes"/"No", "deceasedRecords",
      "names": {"name": [{firstName, middleName, lastName, ...}]},
      "DOBs": {"DOB": "MM/DD/YYYY" or a list of them},
      "addresses": {"address": [{fullStreet, apt, city, county, state, zip, plusFour, firstDate, lastDate}]},
      "phones": {"phone": [{phoneNumber, phoneType, carrier, firstDate: {day, month, year}, ...}]},
      "relationships": {"relationship": [{relationshipType, name: {...}, DOB, currentAge, reportToken}]},
      "emailRecords": ...}]}}
  and with nothing found: {"Status": "NORESULTS", "Data": null, "Error": null}.
The documented alternatives (DODs, relatives.relative[], line1..line3, houseNumber/streetName) are read
too, and every collection is read the same tolerant way: a list, one object, or a wrapper.
"""

import re


def items(node, key=None):
    """A list of dicts from node[key]: accepts a list, one dict, or a wrapper like {"name": [...]}."""
    value = node.get(key) if (key and isinstance(node, dict)) else node
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for entry in value:
            out.extend(items(entry))
        return out
    if isinstance(value, dict):
        inner = [v for v in value.values() if isinstance(v, (list, dict))]
        # A wrapper holds exactly one collection and nothing else, e.g. {"name": [...]}.
        if len(value) == 1 and inner:
            return items(inner[0])
        return [value]
    return []


def _text(value):
    value = "" if value is None else str(value).strip()
    return value or None


def name_of(entry):
    parts = [entry.get(k) for k in ("firstName", "middleName", "lastName", "nameSuffix")]
    return " ".join(str(p).strip() for p in parts if p and str(p).strip()) or None


US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _date(entry):
    """'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY' from {"year", "month", "day"}, 'MM/DD/YYYY' or an ISO string."""
    if isinstance(entry, str):
        us = US_DATE_RE.match(entry.strip())
        if us:
            month, day, year = (int(g) for g in us.groups())
            return f"{year:04d}-{month:02d}-{day:02d}" if month else f"{year:04d}"
        return _text(entry)
    if not isinstance(entry, dict):
        return None
    year, month, day = entry.get("year"), entry.get("month"), entry.get("day")
    if not year:
        return _text(entry.get("date") or entry.get("value"))
    out = f"{int(year):04d}"
    if month:
        out += f"-{int(month):02d}"
        if day:
            out += f"-{int(day):02d}"
    return out


def _values(node):
    """Strings and dicts in node, flattening lists: "x" -> ["x"], ["x", {..}] -> ["x", {..}]."""
    if node is None:
        return []
    if isinstance(node, list):
        return [v for entry in node for v in _values(entry)]
    return [node]


def dobs(person):
    """[{"date": "YYYY-MM-DD", "age"}] from DOBs.DOB (a date string or list of them) or DOB."""
    raw = person.get("DOBs")
    if isinstance(raw, dict) and "DOB" in raw:
        raw = raw["DOB"]
    elif raw is None:
        raw = person.get("DOB")
    out = []
    for dob in _values(raw):
        if isinstance(dob, dict):
            out.append({"date": _date(dob.get("DOB", dob)), "age": _int(dob.get("age"))})
        else:
            out.append({"date": _date(dob), "age": None})
    if not out and person.get("currentAge") is not None:
        out.append({"date": None, "age": _int(person.get("currentAge"))})
    return [d for d in out if d["date"] or d["age"]]


def dods(person):
    """Death records from deceasedRecords (live) or DODs (documented)."""
    out = []
    for key in ("deceasedRecords", "DODs", "deceasedRecord"):
        for record in items(person, key):
            dod = record.get("DOD") or record.get("dateOfDeath") or record
            out.append({"date": _date(dod) if not isinstance(dod, dict) or dod is not record else _date(record.get("date")),
                        "age_at_death": _int(record.get("ageAtDeath")), "dob": _date(record.get("DOB"))})
    return [d for d in out if d["date"] or d["age_at_death"]]


def is_deceased(person):
    return str(person.get("deceased") or "").strip().lower() in ("yes", "y", "true", "1")


def _int(value):
    match = re.search(r"\d{1,3}", str(value or ""))
    return int(match.group(0)) if match else None


def street_of(address):
    """'1648 N Ogden Dr Apt 2' from fullStreet / line1-3 / the street parts."""
    line = " ".join(_text(address.get(k)) or "" for k in ("fullStreet", "line1", "line2", "line3")).strip()
    if not line:
        line = " ".join(str(address.get(k)).strip() for k in (
            "houseNumber", "preDirection", "streetPreDirection", "streetName", "streetType", "streetSuffix",
            "postDirection", "streetPostDirection") if address.get(k))
    unit = address.get("apt") or address.get("unit")
    if unit and str(unit) not in line:
        line += f" {unit}"
    return line.strip() or None


def address_text(address):
    line = street_of(address) or ""
    place = ", ".join(p for p in (_text(address.get("city")), " ".join(
        p for p in (_text(address.get("state")), _text(address.get("zip"))) if p)) if p)
    return ", ".join(p for p in (line.strip(), place) if p) or None


def addresses(person):
    out = []
    for address in items(person, "addresses"):
        out.append({
            "address": address_text(address),
            "street": street_of(address),
            "zip": _text(address.get("zip")),
            "city": _text(address.get("city")),
            "county": _text(address.get("county")),
            "state": _text(address.get("state")),
            "first_seen": _date(address.get("firstDate")),
            "last_seen": _date(address.get("lastDate")),
            "phones": phones(address),
        })
    out = [a for a in out if a["address"]]
    return sorted(out, key=lambda a: a["last_seen"] or "", reverse=True)  # most recent first


PHONE_KEYS = ("phoneNumber", "number", "phone", "value")
PHONE_TYPE_KEYS = ("phoneType", "lineType", "type")


def phones(node):
    """[{"number": 10 digits, "type"}] from node["phones"]: strings, objects, or a wrapper of either."""
    raw = node.get("phones") if isinstance(node, dict) else None
    if raw is None:
        return []
    if isinstance(raw, (str, int)):
        entries = [raw]
    elif isinstance(raw, list) and all(isinstance(entry, (str, int)) for entry in raw):
        entries = raw
    else:
        entries = items(node, "phones")

    unique = {}
    for phone in entries:
        if isinstance(phone, (str, int)):
            number, kind, last_seen = str(phone), None, None
        else:
            number = next((str(phone[k]) for k in PHONE_KEYS if phone.get(k)), None)
            kind = next((str(phone[k]) for k in PHONE_TYPE_KEYS if phone.get(k)), None)
            last_seen = _date(phone.get("lastDate"))
        digits = re.sub(r"\D", "", number or "")
        if len(digits) >= 10:
            unique.setdefault(digits[-10:], {"number": digits[-10:], "type": kind, "last_seen": last_seen})
    return list(unique.values())


def most_recent_phones(phones, limit):
    """The `limit` phones seen most recently (by lastDate), newest first; undated ones last."""
    return sorted(phones or [], key=lambda p: p.get("last_seen") or "", reverse=True)[:limit]


def emails(person):
    out = []
    for key in ("emails", "emailRecords"):
        for email in _values(person.get(key)):
            for entry in (items(email) if isinstance(email, dict) else [email]):
                value = (entry.get("emailAddress") or entry.get("email") or entry.get("value")) if isinstance(entry, dict) else entry
                if value and "@" in str(value):
                    out.append(str(value).strip())
    return sorted(set(out))


def names(person):
    found = [name_of(entry) for entry in items(person, "names")]
    if isinstance(person.get("name"), dict):  # a relationship carries one "name" object
        found.append(name_of(person["name"]))
    found = [n for n in found if n]
    return list(dict.fromkeys(found)) or ([name_of(person)] if name_of(person) else [])


def relatives(person):
    out = []
    for relative in items(person, "relationships") + items(person, "relatives"):
        out.append({
            "names": names(relative),
            "age": _int(relative.get("currentAge")),
            "relationship": _text(relative.get("relationshipType")),
            "token": _text(relative.get("relativeReportToken") or relative.get("reportToken")),
            "dobs": dobs(relative),
            "addresses": addresses(relative),
            "phones": phones(relative),
            "emails": emails(relative),
        })
    return out


def person_record(person):
    all_phones = phones(person)
    for address in addresses(person):
        all_phones += [p for p in address["phones"] if p["number"] not in {q["number"] for q in all_phones}]
    return {
        "token": _text(person.get("reportToken")),
        "deceased_flag": is_deceased(person),
        "gender": _text(person.get("gender")),
        "names": names(person),
        "dobs": dobs(person),
        "dods": dods(person),
        "addresses": addresses(person),
        "phones": all_phones,
        "emails": emails(person),
        "relatives": relatives(person),
    }


def find_people(data):
    """Every person record in a response: a dict with a reportToken and names or addresses.
    Relatives inside a person are not returned as separate people."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for entry in node:
                walk(entry)
        elif isinstance(node, dict):
            if node.get("reportToken") and ("names" in node or "addresses" in node or "firstName" in node):
                found.append(node)
                return
            for key, value in node.items():
                if key not in ("relatives", "relationships"):
                    walk(value)

    walk(data)
    return [person_record(person) for person in found]
