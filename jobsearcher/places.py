"""Which county (län) a Swedish municipality or well-known district belongs to.

Platsbanken gives each ad its county, so "Stockholm" in `search.locations` also
matches Södertälje or Solna through "Stockholms län". Company feeds, LinkedIn and
Indeed only give a city; this fills in the county for the location filter, so the
same job passes the same way whatever the source. Covers the counties around the
default search locations; add more when searching elsewhere.
"""

from __future__ import annotations

_COUNTIES: dict[str, tuple[str, ...]] = {
    "Stockholms län": (
        "Botkyrka", "Danderyd", "Ekerö", "Haninge", "Huddinge", "Järfälla", "Lidingö",
        "Nacka", "Norrtälje", "Nykvarn", "Nynäshamn", "Salem", "Sigtuna", "Sollentuna",
        "Solna", "Stockholm", "Sundbyberg", "Södertälje", "Tyresö", "Täby",
        "Upplands Väsby", "Upplands-Bro", "Vallentuna", "Vaxholm", "Värmdö", "Österåker",
        # districts and towns often given instead of the municipality
        "Kista", "Bromma", "Arlanda", "Märsta", "Tumba", "Jakobsberg", "Kungens kurva",
        "Johanneshov", "Hägersten", "Skärholmen", "Farsta", "Vällingby", "Spånga",
        "Barkarby", "Akalla", "Frösunda", "Hagastaden", "Gustavsberg", "Åkersberga",
        "Bandhagen", "Enskede", "Enskededalen", "Älvsjö", "Sickla", "Skogås", "Ösmo",
    ),
    "Uppsala län": (
        "Enköping", "Heby", "Håbo", "Knivsta", "Tierp", "Uppsala", "Älvkarleby",
        "Östhammar", "Bålsta", "Skutskär", "Gimo",
    ),
    "Skåne län": (
        "Bjuv", "Bromölla", "Burlöv", "Båstad", "Eslöv", "Helsingborg", "Hässleholm",
        "Höganäs", "Hörby", "Höör", "Klippan", "Kristianstad", "Kävlinge", "Landskrona",
        "Lomma", "Lund", "Malmö", "Osby", "Perstorp", "Simrishamn", "Sjöbo", "Skurup",
        "Staffanstorp", "Svalöv", "Svedala", "Tomelilla", "Trelleborg", "Vellinge",
        "Ystad", "Åstorp", "Ängelholm", "Örkelljunga", "Östra Göinge", "Hyllie", "Arlöv",
    ),
    "Västra Götalands län": (
        "Ale", "Alingsås", "Bengtsfors", "Bollebygd", "Borås", "Dals-Ed", "Essunga",
        "Falköping", "Färgelanda", "Grästorp", "Gullspång", "Göteborg", "Götene",
        "Herrljunga", "Hjo", "Härryda", "Karlsborg", "Kungälv", "Lerum", "Lidköping",
        "Lilla Edet", "Lysekil", "Mariestad", "Mark", "Mellerud", "Munkedal", "Mölndal",
        "Orust", "Partille", "Skara", "Skövde", "Sotenäs", "Stenungsund", "Strömstad",
        "Svenljunga", "Tanum", "Tibro", "Tidaholm", "Tjörn", "Tranemo", "Trollhättan",
        "Töreboda", "Uddevalla", "Ulricehamn", "Vara", "Vårgårda", "Vänersborg", "Åmål",
        "Öckerö", "Torslanda", "Hisings Backa", "Floby", "Landvetter", "Lindholmen",
    ),
}  # fmt: skip

_BY_PLACE = {place.casefold(): county for county, places in _COUNTIES.items() for place in places}


def county_of(place: str | None) -> str | None:
    """ "Södertälje" -> "Stockholms län"; None when unknown."""
    if not place:
        return None
    return _BY_PLACE.get(place.strip().casefold())
