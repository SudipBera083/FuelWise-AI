# Fuel Optimizer API

A high-performance Django backend API that calculates optimal fuel stops along a driving route in the USA.

## Features
- Calculates the optimal route using the OSRM public API.
- Finds fuel stations along the route using an in-memory KD-Tree for extremely fast spatial queries.
- Optimizes fuel stops using a greedy algorithm to minimize total fuel costs.
- Caches route responses using Django's caching framework.
- Includes a management command to load fuel stations from a CSV file.

## Requirements
- Python 3.10+

## Setup Instructions

1. **Create and Activate a Virtual Environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run Migrations**
   ```bash
   python manage.py makemigrations routing
   python manage.py migrate
   ```

4. **Load Fuel Stations**
   The provided CSV file does not contain latitude/longitude coordinates. The load command uses `geopy` to geocode locations, which is rate-limited to 1 request/second.
   To test the API quickly, you can load a subset of the stations (e.g., 50 stations):
   ```bash
   python manage.py load_fuel_stations "docs/fuel-prices-for-be-assessment.csv" --limit 50
   ```

5. **Run the Development Server**
   ```bash
   python manage.py runserver
   ```

## API Usage

**Endpoint:** `POST /api/optimize-route/`

**Content-Type:** `application/json`

**Request:**
```json
{
    "start": "New York, NY",
    "end": "Los Angeles, CA"
}
```

**Response:**
```json
{
    "distance": 2800.5,
    "total_cost": 540.25,
    "fuel_stops": [
        {
            "location": "WOODSHED OF BIG CABIN, Big Cabin, OK",
            "price": 3.0,
            "gallons": 20.5,
            "cost": 61.5,
            "lat": 36.536,
            "lng": -95.225
        }
    ],
    "route_map": [
        [36.5, -95.2],
        [36.6, -95.1]
    ]
}
```

## Algorithm Details
1. Geocodes the start and end locations using Nominatim.
2. Fetches the driving route polyline from OSRM.
3. Samples waypoints along the route every 50 miles.
4. Uses an in-memory KD-Tree to find candidate fuel stations within ~15 miles of the waypoints.
5. Applies a greedy optimization strategy:
   - If a cheaper station is reachable within 500 miles, buy just enough fuel to reach it.
   - If no cheaper station is reachable, fill the tank completely and drive to the cheapest reachable station.
