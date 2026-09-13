[![GitHub release](https://img.shields.io/github/v/release/dominikgalovic/gtfs2-idsjmk)](https://github.com/dominikgalovic/gtfs2-idsjmk/releases) [![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories/)

# GTFS2 – IDS JMK fork

Fork of [vingerha/gtfs2](https://github.com/vingerha/gtfs2) for the South Moravian integrated transport system (IDS JMK). Everything from the original works the same; this fork adds:

- **Delays from vehicle positions.** The IDS JMK realtime feed publishes vehicle positions but no trip updates, so the original integration shows no delays for it. When a feed has no trip updates, this fork estimates them from where each vehicle is on its trip, for start/end sensors and local stop sensors alike. Feeds that do publish trip updates are used as before.
  - between stops the delay is how late the vehicle left its last stop, and grows only once it is overdue at the next stop; delays are whole minutes rounded down, so under a minute is on time
  - stop ids padded with zeros in the feed are matched (`U01208Z05` = `U1208Z5`)
  - a vehicle waiting at its first stop before departure counts as on time
  - a vehicle reported twice under two labels, or reporting a stop that is not on its trip (e.g. during a diversion), is still placed by its position
  - positions older than 2 minutes are ignored; a trip whose vehicle drops out of the feed for a moment keeps its last estimate for up to 5 minutes
  - a vehicle counts as set off once it moves away from its first stop towards the second between two refreshes; with only one position, one within 300 m of its first stop before departure time is still waiting
  - more than 2 minutes before its first departure a trip never counts as started: the vehicle is still finishing its previous run or parking at the terminus
  - a trip seen underway is not shown as not started again when GPS noise puts it back near its first stop
- **Where the bus is, for local stop sensors.** Each departure in `next_departures_lines` that has a vehicle on its way also carries `vehicle_realtime` (vehicle label), `current_stop_realtime` (the stop it stands at, or the last one it passed) `at_stop_realtime` (true while standing there) and `trip_started_realtime` (false while it is still at or heading to its first stop); `-` when no vehicle is on its way yet.

## Install with HACS

1. Remove the original GTFS2 from HACS first: both use the domain `gtfs2`.
2. HACS → ⋮ → **Custom repositories** → add `https://github.com/dominikgalovic/gtfs2-idsjmk` as type **Integration**.
3. Download **GTFS2 IDS JMK** and restart Home Assistant.

## IDS JMK setup

- New data source URL: `https://kordis-jmk.cz/gtfs/gtfs.zip`
- Sensor options → **Setup Realtime integration?** → `https://kordis-jmk.cz/gtfs/gtfsReal.dat` in both **URL to trip data** and **URL to vehicle position** (local stop sensors only have the first).

## Data and licences

- Timetable and realtime data: KORDIS JMK, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This repository contains no KORDIS JMK data; the integration downloads it from KORDIS JMK. Delays shown for feeds without trip updates are estimated by this integration from vehicle positions and are not official KORDIS JMK data.
- Code: MIT, see [LICENSE](LICENSE). Original integration © @vingerha.

---

*The original GTFS2 README follows.*



# GTFS2 for Static and RealTime Public transport status collecting in Home Assistant
- configuration via the GUI 
- Static schedule on a **route** between start/end stops
- Shows next 10 departures on the same **route-start and route-end**, including alternative transport lines if applicable for the same start/end
- Option to add gtfs **realtime trip updates** source/url
- Option to add gtfs **realtime vehicle location** source/url, generates geojson file which can be used for tracking vehicle on map card
- Option to add gtfs **realtime alerts** source/url
- Add local stops and next departures, based on your location as 'person' or 'zone', can be extended with realtime data 
- A service to update the GTFS static datasource, e.g. for calling the service via automation
- A service to update GTFS real time data locally, reducing internet traffic when using mulitple routes
- A service to update GTFS local stops, e.g. when tied to a moving person
- Allows to load/update/delete datasources in gtfs2 folder from the GUI
- translations: English, French, German, Spanish, Portuguese

**[Documentation](https://github.com/vingerha/gtfs2/wiki)**

![image](https://github.com/vingerha/gtfs2/assets/44190435/401d3f5b-c3c3-405f-ab9a-1ecf949d5428)

## 🌍 Support Environmental Protection

If you would like to show your appreciation for the effort put into this project then please think about supporting environmental protection efforts (as does using public transport) consider donating to one below or any of your own choice:

- 🌱 **Greenpeace**  
  https://www.greenpeace.org/international/donate/

- 🐼 **World Wide Fund for Nature (WWF)**  
  https://donate.worldwildlife.org/

- 🌳 **Rainforest Alliance**  
  https://www.rainforest-alliance.org/donate/



