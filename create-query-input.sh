#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source ../../.venv/bin/activate

python3 scripts/create_query_input.py "$@"

# Examples:
# uv run scripts/create_query_input.py \
#     --nl-query "What is the average elevation of airports in Germany?" \
#     --sql-query "SELECT AVG(elevation_m) \
#                 FROM airports \
#                 INNER JOIN countries ON countries.country = airports.country \
#                 WHERE countries.name = 'Germany'"

# uv run scripts/create_query_input.py \
#     --nl-query "How many chinese woman have won a nobel prize?" \
#     --sql-query "SELECT COUNT(*) \
#                 FROM persons \
#                 INNER JOIN countries ON countries.country = persons.citizenship \
#                 INNER JOIN person_awards ON person_awards.person = persons.person \
#                 INNER JOIN awards ON awards.award  = person_awards.award \
#                 WHERE sex = 'female' \
#                 AND countries.name  = 'People''s Republic of China' \
#                 AND awards.name  LIKE '%nobel%'" \
#     --table-location "awards=all"

# uv run scripts/create_query_input.py \
#     --nl-query "How many mining companies are located either in USA, Canada or Mexico?" \
#     --sql-query "SELECT COUNT(*) \
#                 FROM companies \
#                 INNER JOIN countries ON countries.country = companies.country \
#                 WHERE countries.name IN ('United States', 'Canada', 'Mexico') \
#                 AND companies.industries LIKE '%mining%'"

# uv run scripts/create_query_input.py \
#     --nl-query "How many FIFA World Cup games did England play?" \
#     --sql-query "SELECT COUNT(*) \
#                 FROM matches \
#                 INNER JOIN teams t1 ON t1.team = matches.team1 \
#                 INNER JOIN teams t2 ON t2.team = matches.team2 \
#                 WHERE t1.name = 'England men''s national association football team' \
#                 OR t2.name = 'England men''s national association football team'" \
#     --table-location "matches=all" \
#     --table-location "teams=united-kingdom"

# uv run scripts/create_query_input.py \
#     --nl-query "How many games was Kurt Tschenscher referee in a FIFA World Cup in Europe?" \
#     --sql-query "SELECT COUNT(*) \
#                 FROM matches \
#                 INNER JOIN persons ON persons.person = matches.referee \
#                 INNER JOIN tournaments ON tournaments.tournament = matches.tournament \
#                 INNER JOIN countries ON countries.country = tournaments.country \
#                 INNER JOIN continents ON continents.continent = countries.continent \
#                 WHERE persons.name = 'Kurt Tschenscher' \
#                 AND continents.name = 'Europe'" \
#     --table-location "matches=Europe" \
#     --table-location "persons=all" \
#     --table-location "tournaments=Europe" \
#     --table-location "countries=Europe" \
#     --table-location "continents=Europe"

# uv run scripts/create_query_input.py \
#     --nl-query "When and where did the highest attendance at a stadium occur and how many people attended?" \
#     --sql-query "SELECT venues.name, matches.attendance, matches.date, matches.name \
#                 FROM matches \
#                 INNER JOIN venues ON venues.venue  = matches.venue \
#                 WHERE matches.attendance = (SELECT MAX(attendance) FROM matches)" \
#     --location "all"

# uv run scripts/create_query_input.py \
#     --nl-query "Is the lowest point in Africa or the lowest point in Antarctica lower?" \
#     --sql-query "SELECT CASE \
#                     WHEN c1.lowest_point_m < c2.lowest_point_m THEN c1.name \
#                     ELSE c2.name \
#                 END AS lower_continent \
#                 FROM continents c1, continents c2 \
#                 WHERE c1.name = 'Africa' \
#                 AND c2.name = 'Antarctica'"

# uv run scripts/create_query_input.py \
#     --nl-query "What is the combined area of all continents in square kilometers?" \
#     --sql-query "SELECT SUM(continents.area_km2) \
#                 FROM continents"