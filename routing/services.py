import math
import requests
import numpy as np
from scipy.spatial import cKDTree
from django.core.cache import cache
from geopy.geocoders import Nominatim
from .models import FuelStation

# --- Constants ---
MAX_RANGE_MILES = 500
MPG = 10
TANK_CAPACITY_GALLONS = MAX_RANGE_MILES / MPG  # 50 gallons
METERS_TO_MILES = 0.000621371

geolocator = Nominatim(user_agent="fuel_optimizer_app")

def geocode_location(location_str):
    cache_key = f"geocode_{location_str.lower().replace(' ', '_')}"
    cached_loc = cache.get(cache_key)
    if cached_loc:
        return cached_loc
    
    try:
        location = geolocator.geocode(location_str)
        if location:
            res = (location.latitude, location.longitude)
            cache.set(cache_key, res, timeout=60*60*24*30) # cache for 30 days
            return res
    except Exception as e:
        print(f"Geocoding error for {location_str}: {e}")
    return None

def get_osrm_route(start_coords, end_coords):
    lon1, lat1 = start_coords[1], start_coords[0]
    lon2, lat2 = end_coords[1], end_coords[0]
    url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
    
    resp = requests.get(url)
    if resp.status_code == 200:
        data = resp.json()
        if data.get("routes"):
            route = data["routes"][0]
            distance_miles = route["distance"] * METERS_TO_MILES
            geometry = route["geometry"]["coordinates"] 
            # OSRM returns [lon, lat], convert to [lat, lon]
            poly_latlon = [[pt[1], pt[0]] for pt in geometry]
            return {"distance": distance_miles, "polyline": poly_latlon}
    return None

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8 # Earth radius in miles
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat/2) * math.sin(dLat/2) + \
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * \
        math.sin(dLon/2) * math.sin(dLon/2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

class StationLocator:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(StationLocator, cls).__new__(cls)
            cls._instance._init_kdtree()
        return cls._instance

    def _init_kdtree(self):
        stations = list(FuelStation.objects.exclude(latitude__isnull=True).exclude(longitude__isnull=True).values(
            'id', 'latitude', 'longitude', 'retail_price', 'name', 'address', 'city', 'state'
        ))
        self.stations = stations
        if not stations:
            self.kdtree = None
            return
            
        coords = [(s['latitude'], s['longitude']) for s in stations]
        self.kdtree = cKDTree(coords)
        
    def find_stations_along_route(self, polyline, interval_miles=50):
        if not self.kdtree or not polyline:
            return []
            
        waypoints = []
        accumulated_dist = 0.0
        
        waypoints.append((polyline[0], 0.0))
        
        for i in range(1, len(polyline)):
            pt1 = polyline[i-1]
            pt2 = polyline[i]
            dist = haversine(pt1[0], pt1[1], pt2[0], pt2[1])
            accumulated_dist += dist
            
            if accumulated_dist - waypoints[-1][1] >= interval_miles:
                waypoints.append((pt2, accumulated_dist))
                
        if accumulated_dist - waypoints[-1][1] > 0:
            waypoints.append((polyline[-1], accumulated_dist))
            
        candidate_station_ids = set()
        route_stations = []
        
        for pt, dist_along_route in waypoints:
            # max distance roughly 15 miles ~ 0.25 degrees
            idx = self.kdtree.query_ball_point((pt[0], pt[1]), r=0.25)
            for i in idx:
                s = self.stations[i]
                if s['id'] not in candidate_station_ids:
                    exact_dist = haversine(pt[0], pt[1], s['latitude'], s['longitude'])
                    if exact_dist <= 15.0: 
                        candidate_station_ids.add(s['id'])
                        route_stations.append({
                            'id': s['id'],
                            'name': s['name'],
                            'address': s['address'],
                            'city': s['city'],
                            'state': s['state'],
                            'price': s['retail_price'],
                            'lat': s['latitude'],
                            'lng': s['longitude'],
                            'dist_along_route': dist_along_route
                        })
                        
        route_stations.sort(key=lambda x: x['dist_along_route'])
        return route_stations

def optimize_fuel_stops(route_distance, stations):
    """
    Globally optimal fuel stop planner using O(N²) forward dynamic programming.

    Strategy
    --------
    Build a node list:  [first_station, ..., destination]
    dp_cost[i]  – minimum total fuel spend to *arrive* at node i.
    dp_fuel[i]  – gallons in tank upon arriving at node i (under the optimal policy).
    dp_prev[i]  – index of the node we travelled from.
    dp_buy[i]   – gallons purchased at dp_prev[i] before departing for i.

    For every forward edge j → i (gap ≤ MAX_RANGE_MILES) we decide how many
    gallons to buy at j:
      • If p[i] < p[j]  → buy only the minimum needed to reach i (save money at i).
      • If p[i] >= p[j] → fill the tank at j (stock up at the cheaper price).
    We then cap to the tank capacity and floor to the minimum needed, update dp[i]
    only when the new cost is strictly lower, guaranteeing a globally optimal path.
    """

    # ------------------------------------------------------------------ setup
    dest = {
        'id': -1,
        'dist_along_route': route_distance,
        'price': 0.0,
        'name': 'Destination',
        'city': '',
        'state': '',
        'lat': 0.0,
        'lng': 0.0,
    }

    nodes = [s for s in stations if s['dist_along_route'] < route_distance]
    nodes.append(dest)

    if not nodes or nodes[0]['id'] == -1:
        return 0.0, []

    N = len(nodes)

    # DP tables
    INF = float('inf')
    dp_cost = [INF] * N
    dp_fuel = [0.0] * N   # fuel on arrival
    dp_prev = [-1]  * N
    dp_buy  = [0.0] * N   # gallons bought at dp_prev[i] before leaving for i

    # Start: arrive at node 0 with empty tank, zero cost
    dp_cost[0] = 0.0
    dp_fuel[0] = 0.0

    # ---------------------------------------------------------------- forward DP
    for j in range(N - 1):
        if dp_cost[j] == INF:
            continue  # node j is unreachable – skip

        dist_j  = nodes[j]['dist_along_route']
        price_j = nodes[j]['price']
        fuel_j  = dp_fuel[j]           # gallons on arrival at j

        for i in range(j + 1, N):
            gap        = nodes[i]['dist_along_route'] - dist_j
            if gap > MAX_RANGE_MILES:
                break                  # stations are sorted; no point continuing

            fuel_needed = gap / MPG    # gallons to cover the gap
            price_i     = nodes[i]['price']

            # Bounds on purchase at j
            min_buy = max(0.0, fuel_needed - fuel_j)        # must not run dry
            max_buy = TANK_CAPACITY_GALLONS - fuel_j        # cannot overfill

            if min_buy > max_buy + 1e-9:
                continue               # infeasible (gap > tank range); skip

            # ---- Optimal purchase decision at j when heading to i ----
            # If i is cheaper, defer buying — only purchase what's strictly needed.
            # If i is more expensive (or equal), stock up at j's lower price.
            if price_i < price_j:
                buy = min_buy          # buy as little as possible here
            else:
                buy = max_buy          # fill up at the cheaper current station

            # Clamp for floating-point safety
            buy = max(min_buy, min(buy, max_buy))

            fuel_arriving_i = fuel_j + buy - fuel_needed
            new_cost = dp_cost[j] + buy * price_j

            if new_cost < dp_cost[i] - 1e-9:
                dp_cost[i] = new_cost
                dp_fuel[i] = fuel_arriving_i
                dp_prev[i] = j
                dp_buy[i]  = buy

    # ----------------------------------------- check destination is reachable
    if dp_cost[N - 1] == INF:
        raise ValueError(
            f"Cannot reach the destination within {MAX_RANGE_MILES}-mile tank range."
        )

    # -------------------------------------------------- backtrack & build stops
    path_indices = []
    idx = N - 1
    while idx != -1:
        path_indices.append(idx)
        idx = dp_prev[idx]
    path_indices.reverse()           # origin-first order

    total_cost = 0.0
    stops = []

    for k in range(1, len(path_indices)):
        node_idx  = path_indices[k]
        src_idx   = path_indices[k - 1]
        gallons   = dp_buy[node_idx]

        if gallons < 1e-6:
            continue                  # no fuel bought at source – skip entry

        src    = nodes[src_idx]
        cost   = gallons * src['price']
        total_cost += cost

        # Destination node has no meaningful address
        if src['id'] == -1:
            continue

        stops.append({
            'location': f"{src['name']}, {src['city']}, {src['state']}",
            'price':    src['price'],
            'gallons':  round(gallons, 2),
            'cost':     round(cost, 2),
            'lat':      src['lat'],
            'lng':      src['lng'],
        })

    return total_cost, stops
