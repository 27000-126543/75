import requests, json
BASE = 'http://localhost:8000/api/v1'

def login(username, password):
    r = requests.post(f'{BASE}/auth/login', json={'username': username, 'password': password})
    return {'Authorization': f'Bearer {r.json().get("access_token")}'}

h = login('admin', 'admin123')

r = requests.post(f'{BASE}/personnel/location-report', headers=h, json={'user_id': 6, 'area': 'A采区'})
print('小陈上报:', r.status_code, r.json())
r = requests.post(f'{BASE}/personnel/location-report', headers=h, json={'user_id': 7, 'area': 'B采区'})
print('小刘上报:', r.status_code, r.json())

r = requests.get(f'{BASE}/personnel/location/latest/6', headers=h)
print('小陈最新位置:', r.json())
r = requests.get(f'{BASE}/personnel/location/latest/7', headers=h)
print('小刘最新位置:', r.json())

r = requests.post(f'{BASE}/environment/data', headers=h, json={'area': 'A采区', 'gas_concentration': 0.5, 'dust_concentration': 20.0, 'ventilation_active': False})
res = r.json()
evac_id = res.get('evacuation_order_id')
print('撤离id:', evac_id, '事件id:', res.get('safety_event_id'))

r = requests.get(f'{BASE}/safety/evacuations/{evac_id}/unconfirmed', headers=h)
print('未确认:', json.dumps(r.json(), ensure_ascii=False, indent=2))
