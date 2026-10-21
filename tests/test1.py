
from itertools import combinations

from sentence_transformers import SentenceTransformer
from sentence_transformers.util import cos_sim


# 1. Test records based on your four incidents

# Additional sample reports for testing semantic similarity

records = [
    # Report 5: Same Highway 99 incident as Incident 1
    {
        "id": 5,
        "date": "2026-10-06",
        "location": "Northbound Highway 99 near Howsley Road, Sutter County, California",
        "type": "car_crash",
        "fatality_count": 2,
        "victims": [
            "Gabriel Rodriguez Maldonado, age 45",
            "Maria Cristina Alarcon-Abarca, age 42",
        ],
        "evidence": (
            "A wrong-way driver caused a head-on collision on "
            "Highway 99 in Sutter County on Tuesday evening. "
            "Two Chico residents died at the scene."
        ),
    },

    # Report 6: Same Whittier incident as Incident 2
    {
        "id": 6,
        "date": "2026-10-05",
        "location": "Slauson Avenue and Norwalk Boulevard, Whittier, California",
        "type": "pedestrian_crash",
        "fatality_count": 1,
        "victims": ["Zenia West, age 39"],
        "evidence": (
            "A 39-year-old woman, Zenia West, died after a "
            "vehicle struck her near a restaurant in Whittier."
        ),
    },

    # Report 7: Same Salinas incident as Incident 4
    {
        "id": 7,
        "date": "2026-10-04",
        "location": "Near Salinas, Monterey County, California",
        "type": "car_crash",
        "fatality_count": 3,
        "victims": [
            "Abigail Tinoco, age 19",
            "Sebastian Burgos, age 18",
            "Noah Trevino, age 18",
        ],
        "evidence": (
            "Three young people died Sunday night in a head-on "
            "crash involving a Nissan Altima and a Honda CR-V "
            "outside Salinas."
        ),
    },

    # Report 8: A different fatal accident
    {
        "id": 8,
        "date": "2026-10-03",
        "location": "Interstate 5 near Sacramento, California",
        "type": "car_crash",
        "fatality_count": 1,
        "victims": ["Daniel Example, age 52"],
        "evidence": (
            "A 52-year-old driver died following a collision "
            "on Interstate 5 near Sacramento on Saturday."
        ),
    },

    # Report 9: A different pedestrian accident
    {
        "id": 9,
        "date": "2026-10-02",
        "location": "Market Street, San Francisco, California",
        "type": "pedestrian_crash",
        "fatality_count": 1,
        "victims": ["Alex Example, age 67"],
        "evidence": (
            "A pedestrian was killed after being struck by "
            "a vehicle on Market Street in San Francisco."
        ),
    },

    # Report 10: A different multi-fatality accident
    {
        "id": 10,
        "date": "2026-10-01",
        "location": "Route 101 near Eureka, Humboldt County, California",
        "type": "car_crash",
        "fatality_count": 2,
        "victims": [
            "Jordan Example, age 34",
            "Taylor Example, age 31",
        ],
        "evidence": (
            "Two people died in a vehicle collision on "
            "Route 101 near Eureka in Humboldt County."
        ),
    },
]


# 2. Convert each incident into text for comparison
def incident_to_text(record):
    victims = ", ".join(record["victims"])

    return (
        f"Incident date: {record['date']}. "
        f"Location: {record['location']}. "
        f"Incident type: {record['type']}. "
        f"Fatalities: {record['fatality_count']}. "
        f"Victims: {victims}. "
        f"Evidence: {record['evidence']}"
    )


# 3. Load a free embedding model
print("Loading embedding model...")
model = SentenceTransformer("all-MiniLM-L6-v2")

texts = [incident_to_text(r) for r in records]

# Convert records into numerical embeddings
embeddings = model.encode(texts, normalize_embeddings=True)


# 4. Compare every pair of incidents
print("\nPAIRWISE SIMILARITY RESULTS")
print("=" * 60)

for i, j in combinations(range(len(records)), 2):
    record_a = records[i]
    record_b = records[j]

    score = cos_sim(
        embeddings[i], embeddings[j]
    ).item()

    # Illustrative threshold, not a proven rule
    if score >= 0.70:
        verdict = "POSSIBLE DUPLICATE - REVIEW"
    else:
        verdict = "LIKELY DIFFERENT - REVIEW IF NEEDED"

    print(f"\nIncident {record_a['id']} vs Incident {record_b['id']}")
    print(f"Similarity score: {score:.3f}")
    print(f"Verdict: {verdict}")
    print(f"Location A: {record_a['location']}")
    print(f"Location B: {record_b['location']}")
    print(f"Date A: {record_a['date']}")
    print(f"Date B: {record_b['date']}")
