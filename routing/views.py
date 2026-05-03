from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache
from .serializers import RouteRequestSerializer
from .services import geocode_location, get_osrm_route, StationLocator, optimize_fuel_stops

class OptimizeRouteView(APIView):
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
        start_loc = serializer.validated_data['start']
        end_loc = serializer.validated_data['end']
        
        cache_key = f"route_{start_loc}_{end_loc}".replace(' ', '_').lower()
        cached_result = cache.get(cache_key)
        if cached_result:
            return Response(cached_result)
            
        start_coords = geocode_location(start_loc)
        if not start_coords:
            return Response({"error": f"Could not geocode start location: {start_loc}"}, status=status.HTTP_400_BAD_REQUEST)
            
        end_coords = geocode_location(end_loc)
        if not end_coords:
            return Response({"error": f"Could not geocode end location: {end_loc}"}, status=status.HTTP_400_BAD_REQUEST)
            
        route_data = get_osrm_route(start_coords, end_coords)
        if not route_data:
            return Response({"error": "Could not calculate route"}, status=status.HTTP_400_BAD_REQUEST)
            
        polyline = route_data['polyline']
        distance = route_data['distance']
        
        locator = StationLocator()
        stations = locator.find_stations_along_route(polyline, interval_miles=50)
        
        try:
            total_cost, stops = optimize_fuel_stops(distance, stations)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            
        result = {
            "distance": round(distance, 2),
            "total_cost": round(total_cost, 2),
            "fuel_stops": stops,
            "route_map": polyline
        }
        
        cache.set(cache_key, result, timeout=60*60*24) # cache for 24h
        return Response(result)
