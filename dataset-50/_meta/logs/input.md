# Query input statistics

## Global

- 75 inputs, 116 steps (113 agent steps, 3 coordinator steps), 3409 relevant-agent slots
- agents per query (union over steps): avg 45.5, median 50, min 13, max 124
- agents per step, micro average (coordinator steps ignored): avg 36.0, median 50, min 9, max 50

## Per query

| Query | Steps | Relevant agents | Agents per step |
|---|---|---|---|
| How many FIFA World Cup games did England play? | 2 | 50 | 15, 50 |
| How many NATO members have a GDP less than fifty billion? | 1 | 33 | 33 |
| How many airlines are headquartered in UK? | 2 | 30 | 15, 15 |
| How many airlines have more than ten thousand employees? | 1 | 50 | 50 |
| How many airports are above 3000 meters elevation? | 1 | 50 | 50 |
| How many chinese woman have won a nobel prize? | 5 | 56 | 43, 43, 43, 11, 0 |
| How many countries have a human development index above 0.8? | 1 | 50 | 50 |
| How many countries have a population below fifty million? | 1 | 50 | 50 |
| How many games was Kurt Tschenscher referee in a FIFA World Cup in Europe? | 6 | 124 | 50, 50, 24, 24, 35, 35 |
| How many mining companies are located either in USA, Canada or Mexico? | 2 | 49 | 24, 25 |
| How many nations with a land mass smaller than Saarland are there in each continent? | 3 | 100 | 50, 50, 50 |
| How many people live in the middle east? | 1 | 13 | 13 |
| Is the lowest point in Africa or the lowest point in Antarctica lower? | 2 | 14 | 14, 0 |
| Since how many years do bundesliga clubs exist on average? Today is the 20.02.2032. | 1 | 43 | 43 |
| What is the average area in square kilometers of Chinese provinces? | 1 | 50 | 50 |
| What is the average area of South American countries? | 2 | 14 | 14, 14 |
| What is the average area of US states in square kilometers? | 1 | 50 | 50 |
| What is the average area of countries in square kilometers? | 1 | 50 | 50 |
| What is the average attendance of NBA matches? | 1 | 50 | 50 |
| What is the average attendance of UCL matches? | 1 | 50 | 50 |
| What is the average capacity of UEFA Champions League venues? | 1 | 50 | 50 |
| What is the average elevation of airports in Germany? | 2 | 36 | 23, 13 |
| What is the average elevation of airports in meters? | 1 | 50 | 50 |
| What is the average gini coefficient for each continent? | 2 | 50 | 50, 50 |
| What is the average life expectancy in countries with a population greater than 50 million? | 1 | 50 | 50 |
| What is the average maximum passenger capacity of all aircraft? | 1 | 50 | 50 |
| What is the average net income of companies in the United States? | 2 | 48 | 23, 25 |
| What is the average number of employees of airlines? | 1 | 50 | 50 |
| What is the average number of fatalities per accident? | 1 | 50 | 50 |
| What is the average number of participants in Men's FIFA World Cup? | 1 | 50 | 50 |
| What is the average passenger capacity of all aircraft manufactured by Saab AB? | 1 | 50 | 50 |
| What is the average passenger count of airports in China? | 2 | 22 | 11, 11 |
| What is the average population of German states? | 1 | 50 | 50 |
| What is the average population of countries in Europe? | 2 | 24 | 24, 24 |
| What is the average revenue of companies headquartered in Sweden? | 2 | 23 | 14, 9 |
| What is the average total assets of companies? | 1 | 50 | 50 |
| What is the combined area of all continents in square kilometers? | 1 | 50 | 50 |
| What is the largest province in China by area? | 1 | 50 | 50 |
| What is the median population of German states? Which state is the closest to it? | 1 | 50 | 50 |
| What is the median population of countries in each continent? | 2 | 50 | 50, 50 |
| What is the minimum and maximum population of German states? | 1 | 50 | 50 |
| What is the most populated state in east germany? | 1 | 50 | 50 |
| What is the name and population density of the most populated country in Oceania? | 2 | 14 | 14, 14 |
| What is the name and the size of the capital of the chinese province with the biggest population? | 2 | 61 | 50, 11 |
| What is the total GDP of all countries in Asia? | 2 | 32 | 32, 32 |
| What is the total area in square kilometers of all west german states? | 1 | 50 | 50 |
| What is the total attendance across all matches in the 2026 World cup? | 2 | 26 | 26, 26 |
| What is the total male population of all countries? | 1 | 50 | 50 |
| What is the total number of employees working for Brazilian companies? | 2 | 21 | 11, 10 |
| What is the total number of fatalities in all accidents? | 1 | 50 | 50 |
| What is the total number of goals scored in all tournaments? | 1 | 50 | 50 |
| What is the total number of passengers handled by airports in UK and France? | 2 | 31 | 16, 15 |
| What is the total population of all NATO member countries? | 1 | 33 | 33 |
| What is the total population of countries with a GDP above 1 trillion USD? | 1 | 50 | 50 |
| What was the combined area of the German states that were part of the DDR, measured in square kilometers? | 1 | 50 | 50 |
| What would be the size of the U.S. if we traded Louisiana for Canada? | 3 | 73 | 23, 50, 0 |
| When and where did the highest attendance at a FIFA world cup stadium occur and how many people attended? | 2 | 50 | 50, 50 |
| Which Champions League venue has the highest capacity? | 1 | 50 | 50 |
| Which German state has the largest area? | 1 | 50 | 50 |
| Which NBA team has the largest number of followers? | 1 | 50 | 50 |
| Which UCL club has the most social media followers? | 1 | 50 | 50 |
| Which Women's World cup had the most goals scored? | 1 | 50 | 50 |
| Which aircraft has the highest maximum passenger capacity? | 1 | 50 | 50 |
| Which airport in Africa has the highest passenger count? How many are there? | 3 | 25 | 14, 14, 11 |
| Which city in Spain has the highest population? | 2 | 15 | 15, 15 |
| Which company in Russia has the highest revenue? | 2 | 23 | 12, 11 |
| Which continent has the second largest total population? | 1 | 50 | 50 |
| Which country has the highest GDP in Africa? | 2 | 14 | 14, 14 |
| Which country has the largest population among countries with a life expectancy above 80 years? | 1 | 50 | 50 |
| Which country has the lowest elevation? What is the name and the depth of this point? | 1 | 50 | 50 |
| Which country hosted the most world cup matches? | 3 | 100 | 50, 50, 50 |
| Which country in South America has the highest GDP per capita? | 2 | 14 | 14, 14 |
| Which football club has the most social media followers across the top five european leagues? | 1 | 48 | 48 |
| Which province is the northernmost province in China? | 1 | 50 | 50 |
| Who won the most world cup matches in the last 50 years and how many? | 2 | 50 | 50, 50 |
