# Manual Tasks — Things I Need You To Do

These tasks require Playground access at **ugc.upland.me** or in-game UI checks.
Fill in answers in the `Answer:` lines and I'll incorporate them into the code.

---

## Section 1 — Structure Min-Width Calibration (Playground)

Test each structure on the lot noted. Record pass (fits) or fail (doesn't fit).
If it fits, note whether it takes up the full lot width or leaves noticeable space.

### Group A — Test on 83 or 85 Stobe Ave (~4.8^)

| Structure | Est. min_width | SU | Category | Result |
|---|---|---|---|---|
| Bodega | 4.0^ | 2 | essential | |
| Coffee Stand | 4.0^ | 3 | entertainment | |
| Bike Shop | 4.0^ | 4 | essential | |
| Antique Store | 4.0^ | 3 | essential | |
| Toy Store | 4.0^ | 3 | essential | |
| Bakery | 4.5^ | 3 | entertainment | |
| Arcade | 4.5^ | 3 | entertainment | |
| Pizzeria | 4.5^ | 4 | entertainment | |
| Art Gallery | 4.5^ | 5 | entertainment | |
| Musical Instrument Store | 4.5^ | 4 | essential | |

**Answer (fill in Result column above):**

---

### Group B — Test on 15 Stobe Ave (~5.4^)

| Structure | Est. min_width | SU | Category | Result |
|---|---|---|---|---|
| Tire Shop | 4.5^ | 3 | essential | |
| Pool Hall | 5.0^ | 5 | entertainment | |
| Wheel Alignment Center | 5.0^ | 5 | essential | |

**Answer (fill in Result column above):**

---

### Group C — Test on 114 Seaview (~4.9^)

| Structure | Est. min_width | Notes | Result |
|---|---|---|---|
| Micro Factory | 4.0^ | Key for Zone 5 employment | |
| Office Tower | 5.0^ | Commerce Score | |

**Answer (fill in Result column above):**

---

## Section 2 — SU Value Verification

### Q1: Brewery SU
We have 17 SU in the database. Check the in-game store listing for Brewery.

**Answer:**

---

### Q2: Small Brewery SU
We estimated 9 SU. Check in-game store listing.

**Answer:**

---

### Q3: What is the actual SU for Micro Factory?
Check the in-game store listing.

**Answer:**

---

### Q4: What is the actual SU for Office Tower?
Check the in-game store listing.

**Answer:**

---

## Section 3 — Scoring System Verification (In-Game UI)

### Q5: Office Units in Resident Score
Log into the Upland app and check your current Resident Score breakdown for Dongan Hills.
Do you see "Office Units" or "Commerce Score" as a listed component?

**Answer:**

---

### Q6: Greenery Score Display
Is there an in-game display showing your current Greenery score per neighborhood?
If yes, what does it show for Dongan Hills?

**Answer:**

---

### Q7: Transportation SU — Vehicles
Does placing a vehicle (car, bus, etc.) on a property generate Transportation SU?
If yes, which vehicle types generate the most? What's the SU value shown in the store?

**Answer:**

---

### Q8: Day Care Center Door Orientation
You have a Day Care Center on a property where the door faces away from the street.
Does this affect SU scoring in any visible way (check score breakdown, tooltip, etc.)?

**Answer:**

---

## Section 4 — Farm Structures

### Q9: Farm lot designation
Can any property host farm structures, or do they require a special designation?
Try placing a farm structure (e.g., Farmhouse, Crop Field) on one of your standard DH properties in Playground.

**Answer:**

---

## Section 5 — Structures You Own That Aren't In Our Database

### Q10: Any unusual structures on DH properties?
Go through all your Dongan Hills properties in Playground. List any structures you see that aren't in this list:

> Apartment Building, Day Care Center, Ice Rink, Fire Station, Small Office, Dollar Store,
> Funeral Home, Car Rental, Auto Repair, Try Harder Gym, Police Detention Center,
> Brewery, Small Brewery, Micro Factory, Office Tower, Town House, Small Town House, Micro House

**Answer (list structure name + which property):**

---

## Section 6 — General Map Testing

### Q11: Rosebank general map
Run this command and tell me what recommendations come out:
```
cd /Users/matt.pugliese/projects/local/upland/neighborhood-map
python3 neighborhood_map.py "Rosebank" --city "Staten Island"
```
Do the recommendations look reasonable for that neighborhood?

**Answer:**

---

### Q12: Non-NYC neighborhood test
Try a neighborhood in Chicago or San Francisco:
```
python3 neighborhood_map.py "Lincoln Park" --city "Chicago"
```
Does it run without errors? Do the zone assignments look right?

**Answer:**

---

## Section 7 — Web App Testing

### Q13: Web app local run
```
cd /Users/matt.pugliese/projects/local/upland/webapp
python3 app.py
```
Then open http://localhost:5000. Does the main form load? Can you search for "Dongan Hills"?

**Answer:**

---

### Q14: Map generation via web app
From the web app, generate a map for Dongan Hills with your username.
Does it show recommendations on non-owned properties (it should now, after the fetch_api_dims fix)?

**Answer:**

---
