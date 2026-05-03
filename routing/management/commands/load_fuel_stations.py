import csv
import time
from pathlib import Path
from django.core.management.base import BaseCommand
from geopy.geocoders import Nominatim
from routing.models import FuelStation

class Command(BaseCommand):
    help = 'Load fuel stations from CSV file'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='Path to the CSV file')
        parser.add_argument('--limit', type=int, help='Limit the number of records to import (for testing)')
        parser.add_argument('--skip-geocoding', action='store_true', help='Skip geocoding if you just want to load data fast')

    def handle(self, *args, **options):
        csv_file = options['csv_file']
        limit = options['limit']
        skip_geo = options['skip_geocoding']

        if not Path(csv_file).exists():
            self.stdout.write(self.style.ERROR(f"File {csv_file} does not exist."))
            return

        geolocator = Nominatim(user_agent="fuel_optimizer_loader")
        
        count = 0
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if limit and count >= limit:
                    break
                    
                try:
                    opis_id = int(row['OPIS Truckstop ID'])
                except (ValueError, KeyError):
                    continue
                
                if FuelStation.objects.filter(opis_id=opis_id).exists():
                    continue

                lat, lng = None, None
                if not skip_geo:
                    location_query = f"{row['Address']}, {row['City']}, {row['State']}"
                    try:
                        loc = geolocator.geocode(location_query, timeout=10)
                        if not loc:
                            loc = geolocator.geocode(f"{row['City']}, {row['State']}", timeout=10)
                        
                        if loc:
                            lat = loc.latitude
                            lng = loc.longitude
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(f"Geocode failed for {location_query}: {e}"))
                    
                    time.sleep(1.1)
                
                FuelStation.objects.create(
                    opis_id=opis_id,
                    name=row['Truckstop Name'],
                    address=row['Address'],
                    city=row['City'],
                    state=row['State'],
                    rack_id=int(row['Rack ID']) if row.get('Rack ID') else None,
                    retail_price=float(row['Retail Price']),
                    latitude=lat,
                    longitude=lng
                )
                
                count += 1
                if count % 10 == 0:
                    self.stdout.write(self.style.SUCCESS(f"Loaded {count} stations..."))

        self.stdout.write(self.style.SUCCESS(f'Successfully loaded {count} fuel stations.'))
