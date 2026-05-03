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
    total_cost = 0.0
    stops = []
    
    dest = {
        'id': -1,
        'dist_along_route': route_distance,
        'price': 0.0,
        'name': 'Destination',
        'city': '',
        'state': '',
        'lat': 0.0,
        'lng': 0.0
    }
    
    stations = [s for s in stations if s['dist_along_route'] < route_distance]
    stations.append(dest)
    
    current_fuel = 0.0
    
    if not stations or stations[0]['id'] == -1:
        return 0.0, []
        
    current_station_idx = 0
    current_dist = stations[0]['dist_along_route']
    
    while current_station_idx < len(stations) - 1:
        curr_station = stations[current_station_idx]
        
        reachable = []
        for j in range(current_station_idx + 1, len(stations)):
            if stations[j]['dist_along_route'] - current_dist <= MAX_RANGE_MILES:
                reachable.append((j, stations[j]))
            else:
                break
                
        if not reachable:
            raise ValueError(f"Cannot reach next station from {curr_station['name']} (gap > {MAX_RANGE_MILES} miles).")
            
        cheaper_station_idx = None
        for j, s in reachable:
            if s['price'] < curr_station['price']:
                cheaper_station_idx = j
                break
                
        if cheaper_station_idx is not None:
            next_s = stations[cheaper_station_idx]
            dist_to_next = next_s['dist_along_route'] - current_dist
            fuel_needed = dist_to_next / MPG
            
            fuel_to_buy = max(0.0, fuel_needed - current_fuel)
            if fuel_to_buy > 0:
                cost = fuel_to_buy * curr_station['price']
                total_cost += cost
                stops.append({
                    'location': f"{curr_station['name']}, {curr_station['city']}, {curr_station['state']}",
                    'price': curr_station['price'],
                    'gallons': round(fuel_to_buy, 2),
                    'cost': round(cost, 2),
                    'lat': curr_station['lat'],
                    'lng': curr_station['lng']
                })
                current_fuel += fuel_to_buy
            
            current_dist = next_s['dist_along_route']
            current_fuel -= dist_to_next / MPG
            current_station_idx = cheaper_station_idx
            
        else:
            fuel_to_buy = TANK_CAPACITY_GALLONS - current_fuel
            if fuel_to_buy > 0:
                cost = fuel_to_buy * curr_station['price']
                total_cost += cost
                stops.append({
                    'location': f"{curr_station['name']}, {curr_station['city']}, {curr_station['state']}",
                    'price': curr_station['price'],
                    'gallons': round(fuel_to_buy, 2),
                    'cost': round(cost, 2),
                    'lat': curr_station['lat'],
                    'lng': curr_station['lng']
                })
                current_fuel = TANK_CAPACITY_GALLONS
                
            min_price_idx = reachable[0][0]
            min_price = reachable[0][1]['price']
            for j, s in reachable:
                if s['price'] <= min_price:
                    min_price = s['price']
                    min_price_idx = j
                    
            next_s = stations[min_price_idx]
            dist_to_next = next_s['dist_along_route'] - current_dist
            
            current_dist = next_s['dist_along_route']
            current_fuel -= dist_to_next / MPG
            current_station_idx = min_price_idx
            
    return total_cost, stops
