# AI Agent Instructions for Commute Project

## Project Overview
This is a Flask web application for managing employee commute data and visualizing routes on interactive maps. The application handles employee location data via postcodes and can display/export commute routes.

## Key Components

### Data Flow
1. User uploads employee data (CSV/Excel) with format:
   - Required columns: `Employee Number`, `postcode`
2. User uploads route data (CSV/Excel) with format:
   - Required columns: `Employee name`, `start_postcode`, `end_postcode`
3. Application geocodes postcodes to coordinates using Nominatim API
4. Routes are calculated using OSRM (OpenStreetMap Routing Machine)
5. Results are visualized on interactive Folium maps

### Core Services
- Geocoding: `get_coordinates()` using Nominatim with in-memory caching
- Route calculation: `get_driving_route()` attempts local OSRM first (port 5000), falls back to public OSRM
- Data processing: Pandas DataFrames for data manipulation
- Visualization: Folium for map generation with markers and route lines

## Development Workflow

### Environment Setup
```bash
pip install -r requirements.txt
```
Required packages: Flask, pandas, folium, geopy, requests, openpyxl, xlrd

### Project Structure
- `Commute.py` - Main application file with all routes and business logic 
- `templates/` - HTML templates (upload.html, results.html)
- `static/` - Generated map files and static assets
- Session storage used for maintaining data between requests

### Conventions
1. **Geocoding Cache**: Module-level `geocode_cache` dictionary for performance
2. **Route Drawing**:
   - Blue solid lines for successful OSRM routes
   - Gray dashed lines for direct point-to-point fallback
3. **File Handling**:
   - Supports both Excel (.xlsx, .xls) and CSV formats
   - Case-insensitive column name matching for route files

## Common Development Tasks

### Adding New Data Processing
1. Add validation in appropriate route handler (`upload_file()` or `upload_route()`)
2. Update DataFrame processing to handle new fields
3. Modify templates to display new data
4. Update export functions if needed (`export_csv()` or `export_route_csv()`)

### Map Customization
- Map customization happens in route handlers using Folium
- Default zoom level: 10 for overview, 13 for centered view
- Marker popups contain employee info in HTML format

## Integration Points
1. **Geocoding**: Nominatim API (rate-limited, requires polite `user_agent`)
2. **Routing**: OSRM service (local preferred, public fallback)
3. **File Formats**: Excel via openpyxl/xlrd, CSV via pandas

## Notes
- Debug mode enabled in development
- Secret key generated at runtime for session management
- Auto-creates required directories (static/, templates/)
- Uses Bootstrap and Leaflet for frontend components