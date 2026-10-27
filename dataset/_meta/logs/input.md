# Query input statistics

## Global

- 75 inputs, 116 steps (113 agent steps, 3 coordinator steps), 1225 relevant-agent slots
- agents per query (union over steps): avg 16.3, median 15, min 6, max 44
- agents per step, micro average (coordinator steps ignored): avg 13.1, median 15, min 2, max 15

## Per query

| Query | Steps | Relevant agents | Agents per step |
|---|---|---|---|
| How many FIFA World Cup games did England play? | 2 | 15 | 5, 15 |
| How many NATO members have a GDP less than fifty billion? | 1 | 15 | 15 |
| How many airlines are headquartered in UK? | 2 | 19 | 14, 5 |
| How many airlines have more than ten thousand employees? | 1 | 15 | 15 |
| How many airports are above 3000 meters elevation? | 1 | 15 | 15 |
| How many chinese woman have won a nobel prize? | 5 | 19 | 3, 4, 4, 14, 0 |
| How many countries have a human development index above 0.8? | 1 | 15 | 15 |
| How many countries have a population below fifty million? | 1 | 15 | 15 |
| How many games was Kurt Tschenscher referee in a FIFA World Cup in Europe? | 6 | 44 | 15, 15, 14, 14, 12, 12 |
| How many mining companies are located either in USA, Canada or Mexico? | 2 | 19 | 15, 4 |
| How many nations with a land mass smaller than Saarland are there in each continent? | 3 | 30 | 15, 15, 15 |
| How many people live in the middle east? | 1 | 13 | 13 |
| Is the lowest point in Africa or the lowest point in Antarctica lower? | 2 | 14 | 14, 0 |
| Since how many years do bundesliga clubs exist on average? Today is the 20.02.2032. | 1 | 6 | 6 |
| What is the average area in square kilometers of Chinese provinces? | 1 | 15 | 15 |
| What is the average area of South American countries? | 2 | 14 | 14, 14 |
| What is the average area of US states in square kilometers? | 1 | 15 | 15 |
| What is the average area of countries in square kilometers? | 1 | 15 | 15 |
| What is the average attendance of NBA matches? | 1 | 15 | 15 |
| What is the average attendance of UCL matches? | 1 | 15 | 15 |
| What is the average capacity of UEFA Champions League venues? | 1 | 15 | 15 |
| What is the average elevation of airports in Germany? | 2 | 19 | 14, 5 |
| What is the average elevation of airports in meters? | 1 | 15 | 15 |
| What is the average gini coefficient for each continent? | 2 | 15 | 15, 15 |
| What is the average life expectancy in countries with a population greater than 50 million? | 1 | 15 | 15 |
| What is the average maximum passenger capacity of all aircraft? | 1 | 15 | 15 |
| What is the average net income of companies in the United States? | 2 | 19 | 15, 4 |
| What is the average number of employees of airlines? | 1 | 15 | 15 |
| What is the average number of fatalities per accident? | 1 | 15 | 15 |
| What is the average number of participants in Men's FIFA World Cup? | 1 | 15 | 15 |
| What is the average passenger capacity of all aircraft manufactured by Saab AB? | 1 | 15 | 15 |
| What is the average passenger count of airports in China? | 2 | 17 | 14, 3 |
| What is the average population of German states? | 1 | 15 | 15 |
| What is the average population of countries in Europe? | 2 | 14 | 14, 14 |
| What is the average revenue of companies headquartered in Sweden? | 2 | 17 | 14, 3 |
| What is the average total assets of companies? | 1 | 15 | 15 |
| What is the combined area of all continents in square kilometers? | 1 | 15 | 15 |
| What is the largest province in China by area? | 1 | 15 | 15 |
| What is the median population of German states? Which state is the closest to it? | 1 | 15 | 15 |
| What is the median population of countries in each continent? | 2 | 15 | 15, 15 |
| What is the minimum and maximum population of German states? | 1 | 15 | 15 |
| What is the most populated state in east germany? | 1 | 15 | 15 |
| What is the name and population density of the most populated country in Oceania? | 2 | 14 | 14, 14 |
| What is the name and the size of the capital of the chinese province with the biggest population? | 2 | 29 | 15, 14 |
| What is the total GDP of all countries in Asia? | 2 | 14 | 14, 14 |
| What is the total area in square kilometers of all west german states? | 1 | 15 | 15 |
| What is the total attendance across all matches in the 2026 World cup? | 2 | 15 | 8, 15 |
| What is the total male population of all countries? | 1 | 15 | 15 |
| What is the total number of employees working for Brazilian companies? | 2 | 15 | 13, 2 |
| What is the total number of fatalities in all accidents? | 1 | 15 | 15 |
| What is the total number of goals scored in all tournaments? | 1 | 15 | 15 |
| What is the total number of passengers handled by airports in UK and France? | 2 | 19 | 14, 5 |
| What is the total population of all NATO member countries? | 1 | 15 | 15 |
| What is the total population of countries with a GDP above 1 trillion USD? | 1 | 15 | 15 |
| What was the combined area of the German states that were part of the DDR, measured in square kilometers? | 1 | 15 | 15 |
| What would be the size of the U.S. if we traded Louisiana for Canada? | 3 | 30 | 15, 15, 0 |
| When and where did the highest attendance at a FIFA world cup stadium occur and how many people attended? | 2 | 15 | 15, 15 |
| Which Champions League venue has the highest capacity? | 1 | 15 | 15 |
| Which German state has the largest area? | 1 | 15 | 15 |
| Which NBA team has the largest number of followers? | 1 | 15 | 15 |
| Which UCL club has the most social media followers? | 1 | 15 | 15 |
| Which Women's World cup had the most goals scored? | 1 | 15 | 15 |
| Which aircraft has the highest maximum passenger capacity? | 1 | 15 | 15 |
| Which airport in Africa has the highest passenger count? How many are there? | 3 | 18 | 14, 14, 4 |
| Which city in Spain has the highest population? | 2 | 13 | 13, 13 |
| Which company in Russia has the highest revenue? | 2 | 16 | 14, 2 |
| Which continent has the second largest total population? | 1 | 15 | 15 |
| Which country has the highest GDP in Africa? | 2 | 14 | 14, 14 |
| Which country has the largest population among countries with a life expectancy above 80 years? | 1 | 15 | 15 |
| Which country has the lowest elevation? What is the name and the depth of this point? | 1 | 15 | 15 |
| Which country hosted the most world cup matches? | 3 | 30 | 15, 15, 15 |
| Which country in South America has the highest GDP per capita? | 2 | 14 | 14, 14 |
| Which football club has the most social media followers across the top five european leagues? | 1 | 15 | 15 |
| Which province is the northernmost province in China? | 1 | 15 | 15 |
| Who won the most world cup matches in the last 50 years and how many? | 2 | 15 | 15, 15 |
