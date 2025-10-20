from flask import Flask, render_template, request, jsonify, session, Response, send_file
import json
import pandas as pd
from jinja2.runtime import Undefined
import folium
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
import requests
import os
import secrets
from io import StringIO

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)  # Set a secret key for session

# Ensure the required directories exist
export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'export')
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

if not os.path.exists(static_dir):
    os.makedirs(static_dir)
if not os.path.exists(templates_dir):
    os.makedirs(templates_dir)

# simple in-memory cache for geocoding (module-level, not persisted)
geocode_cache = {}

def unpack_geom(g):
    if isinstance(g, str):
        try:
            return json.loads(g)
        except Exception:
            return None
    return g

def get_coordinates(postcode):
    if not postcode or str(postcode).strip() == '':
        return None
    key = str(postcode).strip().upper()
    if key in geocode_cache:
        return geocode_cache[key]
    try:
        geolocator = Nominatim(user_agent="my_app")
        location = geolocator.geocode(f"{postcode}, UK")
        if location:
            coord = (location.latitude, location.longitude)
            geocode_cache[key] = coord
            return coord
        geocode_cache[key] = None
        return None
    except GeocoderTimedOut:
        # don't cache timeouts; allow retry
        return None


def get_driving_route(start_coord, end_coord):
    """Attempt to get driving route (polyline of lat/lon) from local OSRM or public OSRM server.
    Returns list of [lat, lon] pairs including start and end, or None on failure.
    """
    if not start_coord or not end_coord:
        return None
    # Prefer local OSRM if available, otherwise use public router.project-osrm.org
    base_urls = [
        "http://127.0.0.1:5000/route/v1/driving/",  # local OSRM default
        "https://router.project-osrm.org/route/v1/driving/"
    ]
    coords = f"{start_coord[1]},{start_coord[0]};{end_coord[1]},{end_coord[0]}"
    params = {
        'overview': 'full',
        'geometries': 'geojson'
    }
    for base in base_urls:
        try:
            url = base + coords
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if 'routes' in data and len(data['routes']) > 0:
                    geom = data['routes'][0]['geometry']
                    # geometry is GeoJSON LineString -> coordinates as [lon, lat]
                    path = [[pt[1], pt[0]] for pt in geom['coordinates']]
                    return path
        except Exception:
            continue
    return None


def _append_message_listener_to_map(html_path):
        """Append a small JS listener to a saved folium HTML file so the parent page
        can postMessage commands to fly the map or draw a route.
        """
        try:
                listener = r"""
<script>
;(function(){
    function findMap(){
        if(window._foundMap) return window._foundMap;
        try{
            for(var k in window){
                try{
                    if(window[k] && window[k] instanceof L.Map){ window._foundMap = window[k]; return window._foundMap; }
                }catch(e){}
            }
        }catch(e){}
        return null;
    }

    function clearDynamic(){
        if(window._dynamicLayer){ try{ window._map.removeLayer(window._dynamicLayer); }catch(e){} window._dynamicLayer = null; }
    }

        window.addEventListener('message', function(e){
        var msg = e.data;
        if(!msg || !msg.action) return;
        var _map = findMap();
        if(!_map) return;
        if(msg.action === 'flyTo' && typeof msg.lat === 'number' && typeof msg.lng === 'number'){
            clearDynamic();
            _map.setView([msg.lat, msg.lng], msg.zoom || 13);
            } else if(msg.action === 'showRoute'){
            clearDynamic();
            var layer = null;
            if(msg.route && Array.isArray(msg.route)){
                layer = L.polyline(msg.route, {color:'blue', weight:5}).addTo(_map);
                _map.fitBounds(layer.getBounds());
            } else if(msg.start && msg.end){
                layer = L.polyline([msg.start, msg.end], {color:'gray', weight:3, dashArray:'5'}).addTo(_map);
                _map.fitBounds(layer.getBounds());
            }
            window._dynamicLayer = layer;
            } else if(msg.action === 'fitBounds' && Array.isArray(msg.points) && msg.points.length > 0){
                clearDynamic();
                try{
                    var bounds = L.latLngBounds(msg.points);
                    _map.fitBounds(bounds);
                }catch(e){}
        }
    });
})();
</script>
"""
                # Only append if not already present
                with open(html_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                if 'window.addEventListener(\'message\'' in content:
                        return
                # append before closing </body>
                content = content.replace('</body>', listener + '\n</body>')
                with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(content)
        except Exception:
                # if anything goes wrong, ignore; map will still work but dynamic fly/show won't
                pass

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            return 'No file uploaded'
        file = request.files['file']
        if file.filename == '':
            return 'No file selected'

        file_extension = os.path.splitext(file.filename)[1].lower()
        if file_extension in ['.xlsx', '.xls']:
            df = pd.read_excel(file)
        elif file_extension == '.csv':
            df = pd.read_csv(file)
        else:
            return 'Invalid file format. Please upload an Excel (.xlsx, .xls) or CSV (.csv) file'

        # Normalize columns
        df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
        if 'employee_number' not in df.columns or 'postcode' not in df.columns:
            return 'File must contain Employee Number and postcode columns', 400

        # Geocode postcodes
        df['latitude'] = df['postcode'].apply(lambda x: get_coordinates(x)[0] if get_coordinates(x) else None)
        df['longitude'] = df['postcode'].apply(lambda x: get_coordinates(x)[1] if get_coordinates(x) else None)

        # Save employee data to session
        session['employee_data'] = df.to_dict('records')
        employee_numbers = df['employee_number'].dropna().unique().tolist()

        # Generate map of all employees
        if len(df) > 0:
            m = folium.Map(location=[df['latitude'].mean(), df['longitude'].mean()], zoom_start=10)
            for _, row in df.iterrows():
                if pd.notna(row['latitude']) and pd.notna(row['longitude']):
                    folium.Marker([row['latitude'], row['longitude']], popup=f"Employee: {row['employee_number']}<br>Postcode: {row['postcode']}").add_to(m)
            map_path = os.path.join(static_dir, 'employee_map.html')
            m.save(map_path)
            _append_message_listener_to_map(map_path)
            map_url = '/static/employee_map.html'
        else:
            map_url = 'about:blank'

        # prepare lists and embed coords
        employees_list = df['employee_number'].dropna().unique().tolist()
        employees_coords = {}
        for _, r in df.iterrows():
            try:
                lat = r.get('latitude')
                lng = r.get('longitude')
                if lat is not None and lng is not None and not isinstance(lat, Undefined) and not isinstance(lng, Undefined):
                    employees_coords[int(r['employee_number'])] = {'lat': float(lat), 'lng': float(lng)}
            except Exception:
                continue
        route_employees = session.get('route_data') and [r.get('employee_number') for r in session.get('route_data')] or []
        route_coords = {}
        if 'route_data' in session:
            try:
                rdf = pd.DataFrame(session['route_data'])
                for _, r in rdf.iterrows():
                    try:
                        geom = None
                        if 'route_geometry' in r and pd.notna(r.get('route_geometry')):
                            try:
                                geom = r['route_geometry']
                                # decode if it's a JSON string
                                if isinstance(geom, str):
                                    geom = json.loads(geom)
                            except Exception:
                                geom = None

                        route_coords[int(r['employee_number'])] = {
                            'start': [float(r['start_latitude']), float(r['start_longitude'])]
                                if pd.notna(r['start_latitude']) and pd.notna(r['start_longitude']) else None,
                            'end':   [float(r['end_latitude']), float(r['end_longitude'])]
                                if pd.notna(r['end_latitude']) and pd.notna(r['end_longitude']) else None,
                            'route': geom
                        }

                    except Exception:
                        continue
            except Exception:
                route_coords = {}
        return render_template('dashboard.html', map_url=map_url, employees=employees_list, route_employees=route_employees, employees_coords=employees_coords, route_coords=route_coords)

    # GET: show dashboard with current map if any
    map_url = None
    if os.path.exists(os.path.join(static_dir, 'employee_map.html')):
        map_url = '/static/employee_map.html'
    elif os.path.exists(os.path.join(static_dir, 'route_map.html')):
        map_url = '/static/route_map.html'
    else:
        map_url = 'about:blank'
    employees_list = None
    employees_coords = {}
    if 'employee_data' in session:
        try:
            edf = pd.DataFrame(session['employee_data'])
            employees_list = edf['employee_number'].dropna().unique().tolist()
            for _, r in edf.iterrows():
                try:
                    employees_coords[int(r['employee_number'])] = {'lat': float(r['latitude']), 'lng': float(r['longitude'])}
                except Exception:
                    continue
        except Exception:
            employees_list = []
    route_employees = None
    route_coords = {}
    if 'route_data' in session:
        try:
            rdf = pd.DataFrame(session['route_data'])
            route_employees = rdf['employee_number'].dropna().unique().tolist()
            for _, r in rdf.iterrows():
                try:
                    # geometry: may be JSON string or Python list
                    geom = r.get('route_geometry')
                    geom = unpack_geom(geom)

                    start_lat = r.get('start_latitude')
                    start_lng = r.get('start_longitude')
                    end_lat   = r.get('end_latitude')
                    end_lng   = r.get('end_longitude')

                    # Only include if not Undefined and not None
                    start = (
                        [float(start_lat), float(start_lng)]
                        if (start_lat is not None and start_lng is not None
                            and not isinstance(start_lat, Undefined)
                            and not isinstance(start_lng, Undefined))
                        else None
                    )
                    end = (
                        [float(end_lat), float(end_lng)]
                        if (end_lat is not None and end_lng is not None
                            and not isinstance(end_lat, Undefined)
                            and not isinstance(end_lng, Undefined))
                        else None
                    )

                    route = geom if (geom is not None and not isinstance(geom, Undefined)) else None

                    route_coords[int(r['employee_number'])] = {
                        'start': start,
                        'end': end,
                        'route': route
                    }
                except Exception:
                    continue
        except Exception:
            route_employees = []
    return render_template(
    'dashboard.html',
    map_url=map_url,
    employees=employees_list,
    route_employees=route_employees,
    employees_coords=employees_coords,
    route_coords=route_coords
)

@app.route('/upload_route', methods=['POST'])
def upload_route():

    if 'route_file' not in request.files:
        return 'No route file uploaded', 400
    file = request.files['route_file']
    if file.filename == '':
        return 'No route file selected', 400
    file_extension = os.path.splitext(file.filename)[1].lower()
    if file_extension in ['.xlsx', '.xls']:
        df = pd.read_excel(file)
    elif file_extension == '.csv':
        df = pd.read_csv(file)
    else:
        return 'Invalid file format. Please upload an Excel (.xlsx, .xls) or CSV (.csv) file', 400

    # Normalize column names for flexibility
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]

    # Ensure required columns
    required = ['employee_number', 'start_postcode', 'end_postcode']
    for col in required:
        if col not in df.columns:
            return f'Missing required column: {col}', 400

    # Geocode start and end postcodes
    df['start_latitude'] = df['start_postcode'].apply(lambda x: get_coordinates(x)[0] if get_coordinates(x) else None)
    df['start_longitude'] = df['start_postcode'].apply(lambda x: get_coordinates(x)[1] if get_coordinates(x) else None)
    df['end_latitude'] = df['end_postcode'].apply(lambda x: get_coordinates(x)[0] if get_coordinates(x) else None)
    df['end_longitude'] = df['end_postcode'].apply(lambda x: get_coordinates(x)[1] if get_coordinates(x) else None)

    # Add distance and duration columns using OSRM
    distances = []
    durations = []
    # Pre-create route_geometry column to avoid ValueError
    if 'route_geometry' not in df.columns:
        df['route_geometry'] = None
    for idx, row in df.iterrows():
        start = (row['start_latitude'], row['start_longitude']) if pd.notna(row['start_latitude']) and pd.notna(row['start_longitude']) else None
        end = (row['end_latitude'], row['end_longitude']) if pd.notna(row['end_latitude']) and pd.notna(row['end_longitude']) else None
        distance_miles = None
        duration_hours = None
        route_geom = None
        if start and end:
            # Use OSRM API directly for distance and duration
            base_urls = [
                "http://127.0.0.1:5000/route/v1/driving/",
                "https://router.project-osrm.org/route/v1/driving/"
            ]
            coords = f"{start[1]},{start[0]};{end[1]},{end[0]}"
            params = {'overview': 'false', 'geometries': 'geojson'}
            route_found = False
            for base in base_urls:
                try:
                    url = base + coords
                    resp = requests.get(url, params=params, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        if 'routes' in data and len(data['routes']) > 0:
                            route = data['routes'][0]
                            # OSRM returns distance in meters, duration in seconds
                            distance_miles = round(route['distance'] / 1609.344, 2)
                            duration_hours = round(route['duration'] / 3600, 2)
                            route_found = True
                            break
                except Exception:
                    continue
            # attempt to fetch full geometry for embedding
            try:
                geom = get_driving_route(start, end)
                if geom and isinstance(geom, list) and len(geom) > 1:
                    route_geom = geom
                else:
                    route_geom = None
            except Exception:
                route_geom = None
        # … inside: for idx, row in df.iterrows():
        df.at[idx, 'route_geometry'] = json.dumps(route_geom) if route_geom is not None else None

        distances.append(distance_miles)
        durations.append(duration_hours)
    df['distance_miles'] = distances
    df['duration_hours'] = durations

    # Overwrite static/route_export.csv
    export_cols = ['employee_number', 'start_postcode', 'start_latitude', 'start_longitude', 'end_postcode', 'end_latitude', 'end_longitude', 'distance_miles', 'duration_hours']
    for col in export_cols:
        if col not in df.columns:
            df[col] = None
    export_df = df[export_cols]
    export_path = os.path.join(export_dir, 'route_export.csv')
    export_df.to_csv(export_path, index=False)

    # Save route geometries as GeoJSON and CSV for quick export
    features = []
    for idx, row in df.iterrows():
        geom_val = row.get('route_geometry')
        if isinstance(geom_val, str):
            try:
                geom = json.loads(geom_val)
            except Exception:
                geom = None
        else:
            geom = geom_val if geom_val not in [None, [], {}] else None

        if geom is not None and isinstance(geom, list):
            # geom is list of [lat, lng]; convert to [lng, lat] for GeoJSON
            coords = [[float(pt[1]), float(pt[0])] for pt in geom]
            feat = {
                'type': 'Feature',
                'geometry': {
                    'type': 'LineString',
                    'coordinates': coords
                },
                'properties': {
                    'employee_number': int(row['employee_number']),
                    'start_postcode': str(row['start_postcode']),
                    'end_postcode': str(row['end_postcode']),
                    'distance_miles': float(row['distance_miles']) if pd.notna(row['distance_miles']) else None,
                    'duration_hours': float(row['duration_hours']) if pd.notna(row['duration_hours']) else None
                }
            }
            features.append(feat)
    geojson = {'type': 'FeatureCollection', 'features': features}
    geojson_path = os.path.join(static_dir, 'route_geoms.geojson')
    try:
        with open(geojson_path, 'w', encoding='utf-8') as gf:
            json.dump(geojson, gf)
    except Exception as e:
        print(f"Error saving GeoJSON: {e}")

    # CSV with route geometry as JSON string
    # CSV with route geometry as JSON string
    csv_rows = []
    for _, row in df.iterrows():
        g = row.get('route_geometry')

        # Normalise to a JSON string (or None)
        if isinstance(g, str):
            # already JSON string from earlier df.at[..] = json.dumps(...)
            geom_out = g
        elif g is None:
            geom_out = None
        elif isinstance(g, (list, tuple, dict)):
            # convert Python list/dict to JSON
            try:
                # if it's an empty list/dict, store None for tidier CSV
                geom_out = None if (hasattr(g, '__len__') and len(g) == 0) else json.dumps(g)
            except Exception:
                geom_out = None
        else:
            # handle any other objects (e.g., numpy arrays) without using truthiness
            try:
                geom_out = json.dumps(g)
            except Exception:
                geom_out = None

        csv_rows.append({
            'employee_number': row.get('employee_number'),
            'start_postcode': row.get('start_postcode'),
            'end_postcode': row.get('end_postcode'),
            'distance_miles': row.get('distance_miles'),
            'duration_hours': row.get('duration_hours'),
            'route_geometry': geom_out
        })

    try:
        geo_csv_path = os.path.join(static_dir, 'route_geoms.csv')
        pd.DataFrame(csv_rows).to_csv(geo_csv_path, index=False)
    except Exception:
        pass

    # Save route data to session for later use
    session['route_data'] = df.to_dict('records')
    employee_numbers = df['employee_number'].dropna().unique().tolist()
    first_employee = employee_numbers[0] if employee_numbers else None
    # Generate map for first employee's route (if any)
    if first_employee:
        emp_row = df[df['employee_number'] == first_employee].iloc[0]
        m = folium.Map(location=[emp_row['start_latitude'], emp_row['start_longitude']], zoom_start=12)
        folium.Marker([emp_row['start_latitude'], emp_row['start_longitude']], popup='Start').add_to(m)
        folium.Marker([emp_row['end_latitude'], emp_row['end_longitude']], popup='End').add_to(m)
        start = (emp_row['start_latitude'], emp_row['start_longitude'])
        end = (emp_row['end_latitude'], emp_row['end_longitude'])
        route = get_driving_route(start, end)
        if route:
            folium.PolyLine(route, color='blue', weight=5).add_to(m)
        else:
            folium.PolyLine([
                [emp_row['start_latitude'], emp_row['start_longitude']],
                [emp_row['end_latitude'], emp_row['end_longitude']]
            ], color='gray', weight=3, dash_array='5').add_to(m)
        map_path = os.path.join(static_dir, 'route_map.html')
        m.save(map_path)
        _append_message_listener_to_map(map_path)
        map_url = '/static/route_map.html'
    else:
        map_url = 'about:blank'

    # prepare lists for selector and render
    employees_list = None
    employees_coords = {}
    if 'employee_data' in session:
        try:
            edf = pd.DataFrame(session['employee_data'])
            employees_list = edf['employee_number'].dropna().unique().tolist()
            for _, r in edf.iterrows():
                lat = r.get('latitude')
                lng = r.get('longitude')
                if lat is not None and lng is not None:
                    employees_coords[int(r['employee_number'])] = {'lat': float(lat), 'lng': float(lng)}
        except Exception:
            employees_list = []
    route_employees_list = df['employee_number'].dropna().unique().tolist() if len(df) > 0 else []
    route_coords = {}
    for _, r in df.iterrows():
        try:
            start_lat = r.get('start_latitude')
            start_lng = r.get('start_longitude')
            end_lat   = r.get('end_latitude')
            end_lng   = r.get('end_longitude')

            # normalise geometry: it might be JSON string or already a Python list
            geom = r.get('route_geometry')
            if isinstance(geom, str):
                try:
                    geom = json.loads(geom)
                except Exception:
                    geom = None

            # build start/end safely
            start = [float(start_lat), float(start_lng)] \
                if start_lat is not None and start_lng is not None else None
            end = [float(end_lat), float(end_lng)] \
                if end_lat is not None and end_lng is not None else None

            route_coords[int(r['employee_number'])] = {
                'start': start,
                'end': end,
                'route': geom if geom is not None else None
            }
        except Exception:
            continue

    return render_template('dashboard.html', map_url=map_url, employees=employees_list, route_employees=route_employees_list, employees_coords=employees_coords, route_coords=route_coords)

@app.route('/employee_map/<employee_number>')
def employee_map(employee_number):
    if 'employee_data' not in session:
        return 'No employee data available. Please upload a file first.', 404
    df = pd.DataFrame(session['employee_data'])
    try:
        employee_data = df[df['employee_number'] == int(employee_number)].iloc[0]
    except (IndexError, ValueError):
        return f'Employee {employee_number} not found', 404
    m = folium.Map(location=[employee_data['latitude'], employee_data['longitude']], zoom_start=13)
    folium.Marker([employee_data['latitude'], employee_data['longitude']], popup=f"Employee: {employee_data['employee_number']}<br>Postcode: {employee_data['postcode']}").add_to(m)
    return Response(m._repr_html_(), mimetype='text/html')


@app.route('/api/employee/<employee_number>')
def api_employee(employee_number):
    if 'employee_data' not in session:
        return jsonify({'error': 'no_employee_data'}), 404
    df = pd.DataFrame(session['employee_data'])
    try:
        emp = df[df['employee_number'] == int(employee_number)].iloc[0]
    except (IndexError, ValueError):
        return jsonify({'error': 'not_found'}), 404
    if pd.isna(emp['latitude']) or pd.isna(emp['longitude']):
        return jsonify({'error': 'no_coordinates'}), 400
    return jsonify({'employee_number': int(employee_number), 'lat': float(emp['latitude']), 'lng': float(emp['longitude'])})


@app.route('/api/route/<employee_number>')
def api_route(employee_number):
    if 'route_data' not in session:
        return jsonify({'error': 'no_route_data'}), 404
    df = pd.DataFrame(session['route_data'])
    try:
        emp = df[df['employee_number'] == int(employee_number)].iloc[0]
    except (IndexError, ValueError):
        return jsonify({'error': 'not_found'}), 404
    start_lat = emp.get('start_latitude')
    start_lng = emp.get('start_longitude')
    end_lat = emp.get('end_latitude')
    end_lng = emp.get('end_longitude')
    if pd.isna(start_lat) or pd.isna(start_lng) or pd.isna(end_lat) or pd.isna(end_lng):
        return jsonify({'error': 'no_coordinates'}), 400
    route = get_driving_route((start_lat, start_lng), (end_lat, end_lng))
    if route:
        return jsonify({'route': route})
    # fallback to start/end
    return jsonify({'start': [start_lat, start_lng], 'end': [end_lat, end_lng]})

@app.route('/export_csv')
def export_csv():
    if 'employee_data' not in session:
        return 'No data available. Please upload a file first.', 404
    df = pd.DataFrame(session['employee_data'])
    # Only include required columns (normalized)
    cols = ['employee_number', 'postcode', 'latitude', 'longitude']
    for c in cols:
        if c not in df.columns:
            df[c] = None
    export_df = df[cols]
    csv_buffer = StringIO()
    export_df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    return Response(
        csv_buffer.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': 'attachment; filename=employee_export.csv'
        }
    )

@app.route('/export_route_csv')
def export_route_csv():
    export_path = os.path.join(export_dir, 'route_export.csv')
    if not os.path.exists(export_path):
        return 'No route export available. Please upload a route file first.', 404
    return send_file(export_path, mimetype='text/csv', as_attachment=True, download_name='route_export.csv')


@app.route('/download_route_geoms_geojson')
def download_route_geoms_geojson():
    path = os.path.join(static_dir, 'route_geoms.geojson')
    if not os.path.exists(path):
        return 'No geojson available', 404
    return send_file(path, mimetype='application/geo+json', as_attachment=True, download_name='route_geoms.geojson')


@app.route('/download_route_geoms_csv')
def download_route_geoms_csv():
    path = os.path.join(static_dir, 'route_geoms.csv')
    if not os.path.exists(path):
        return 'No CSV available', 404
    return send_file(path, mimetype='text/csv', as_attachment=True, download_name='route_geoms.csv')

@app.route('/map/<employee_number>')
def map_route(employee_number):
    # Show route for selected employee
    if 'route_data' not in session:
        return 'No route data available. Please upload a route file first.', 404
    df = pd.DataFrame(session['route_data'])
    try:
        employee_data = df[df['employee_number'] == int(employee_number)].iloc[0]
    except (IndexError, ValueError):
        return f'Employee {employee_number} not found', 404
    m = folium.Map(location=[employee_data['start_latitude'], employee_data['start_longitude']], zoom_start=12)
    folium.Marker([employee_data['start_latitude'], employee_data['start_longitude']], popup='Start').add_to(m)
    folium.Marker([employee_data['end_latitude'], employee_data['end_longitude']], popup='End').add_to(m)
    # Try to get driving route
    start = (employee_data['start_latitude'], employee_data['start_longitude'])
    end = (employee_data['end_latitude'], employee_data['end_longitude'])
    route = get_driving_route(start, end)
    if route:
        folium.PolyLine(route, color='blue', weight=5).add_to(m)
    else:
        folium.PolyLine([
            [employee_data['start_latitude'], employee_data['start_longitude']],
            [employee_data['end_latitude'], employee_data['end_longitude']]
        ], color='gray', weight=3, dash_array='5').add_to(m)
    return Response(m._repr_html_(), mimetype='text/html')

if __name__ == '__main__':
    app.run(debug=True)