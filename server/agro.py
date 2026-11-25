"""What a sensor reading means for the farmer's crops.

The same table is written into kb/15_crop_climate_and_sensor_advice.md
(python agro.py --kb), so the answers spoken from a reading and the documents
Gemma retrieves always agree.

Temperatures are a general guide for growing crops in Karnataka (degrees
Celsius, air temperature), compiled for Smart Krishi from standard agronomy
references. Varieties and crop stages differ; the local package of practices
has the final word.
"""
import re

# crop: (spoken name, too cold below, best from, best to, too hot above, water need, main season in Karnataka)
CROPS = {
    "rice":       ("rice (paddy)", 20, 25, 32, 35, "high", "kharif (June to October); summer crop where water is assured"),
    "ragi":       ("ragi (finger millet)", 20, 26, 29, 35, "low", "kharif (June to September sowing)"),
    "maize":      ("maize", 18, 21, 30, 35, "medium", "kharif and rabi"),
    "jowar":      ("jowar (sorghum)", 20, 26, 30, 40, "low", "kharif in the south, rabi in north Karnataka"),
    "bajra":      ("bajra (pearl millet)", 20, 25, 32, 40, "low", "kharif"),
    "wheat":      ("wheat", 10, 15, 22, 30, "medium", "rabi (October to November sowing) in north Karnataka"),
    "redgram":    ("red gram (tur)", 18, 20, 30, 35, "low", "kharif"),
    "bengalgram": ("bengal gram (chickpea)", 10, 15, 25, 30, "low", "rabi"),
    "groundnut":  ("groundnut", 20, 25, 30, 35, "medium", "kharif and summer"),
    "sunflower":  ("sunflower", 15, 20, 25, 35, "medium", "kharif, rabi and summer"),
    "cotton":     ("cotton", 18, 21, 30, 37, "medium", "kharif"),
    "sugarcane":  ("sugarcane", 20, 25, 32, 38, "high", "planted through the year; 12 to 18 month crop"),
    "tomato":     ("tomato", 15, 20, 27, 32, "medium", "all seasons; best from June to February"),
    "onion":      ("onion", 13, 15, 25, 30, "medium", "kharif and rabi"),
    "potato":     ("potato", 10, 15, 20, 30, "medium", "kharif in Hassan and Chikkamagaluru, rabi elsewhere"),
    "chilli":     ("chilli", 18, 20, 30, 35, "medium", "kharif"),
    "brinjal":    ("brinjal", 18, 22, 30, 35, "medium", "all seasons"),
    "cabbage":    ("cabbage and cauliflower", 7, 15, 20, 25, "medium", "rabi (cool months)"),
    "beans":      ("beans", 15, 18, 25, 30, "medium", "kharif and rabi"),
    "banana":     ("banana", 15, 26, 30, 38, "high", "planted through the year"),
    "coconut":    ("coconut", 20, 27, 32, 38, "high", "perennial"),
    "coffee":     ("coffee (arabica)", 15, 15, 25, 30, "medium", "perennial, in the hill districts"),
}

# words a farmer may use for a crop -> key above
ALIASES = {
    "paddy": "rice", "rice": "rice", "ragi": "ragi", "finger millet": "ragi", "maize": "maize", "corn": "maize",
    "jowar": "jowar", "sorghum": "jowar", "bajra": "bajra", "pearl millet": "bajra", "wheat": "wheat",
    "tur": "redgram", "toor": "redgram", "red gram": "redgram", "redgram": "redgram", "pigeon pea": "redgram",
    "bengal gram": "bengalgram", "chickpea": "bengalgram", "gram": "bengalgram", "groundnut": "groundnut",
    "peanut": "groundnut", "sunflower": "sunflower", "cotton": "cotton", "sugarcane": "sugarcane",
    "tomato": "tomato", "onion": "onion", "potato": "potato", "chilli": "chilli", "chili": "chilli",
    "brinjal": "brinjal", "eggplant": "brinjal", "cabbage": "cabbage", "cauliflower": "cabbage",
    "beans": "beans", "banana": "banana", "coconut": "coconut", "coffee": "coffee",
}

# Soil moisture bands for a CALIBRATED sensor (0 = the dry reading, 100 = the wet
# reading set at calibration). A rough guide only: kb/07 explains that each field
# has its own dry and wet readings, and the soil should be checked by hand.
DRY_BELOW, WET_ABOVE = 30, 80


def find_crop(text: str) -> str | None:
    t = (text or "").lower()
    for word in sorted(ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}s?\b", t):
            return ALIASES[word]
    return None


def _list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def temperature_advice(t: float, crop: str | None) -> str:
    """Is this temperature right for the crop, and which crops suit it."""
    out = []
    if crop in CROPS:
        name, cold, lo, hi, hot, *_ = CROPS[crop]
        if t < cold:
            out.append(f"That is too cold for {name}, which grows best between {lo} and {hi} degrees; growth slows and "
                       f"young plants can be damaged, so protect nurseries and delay sowing until it warms up.")
        elif t > hot:
            out.append(f"That is too hot for {name}, which grows best between {lo} and {hi} degrees; flowers and young "
                       f"fruits can drop, so irrigate in the evening, mulch the soil and give shade to nurseries.")
        elif lo <= t <= hi:
            out.append(f"That is a good temperature for {name}, which grows best between {lo} and {hi} degrees.")
        else:
            out.append(f"{name.capitalize()} can grow at this temperature, though it does best between {lo} and {hi} degrees.")
    good = [CROPS[k][0] for k in CROPS if CROPS[k][2] <= t <= CROPS[k][3] and k != crop]
    if good:
        out.append(f"Crops that grow best around this temperature include {_list(good[:6])}.")
    return " ".join(out)


def moisture_advice(pct: float, crop: str | None) -> str:
    """Too dry, about right or too wet, and what to do."""
    name, need = (CROPS[crop][0], CROPS[crop][5]) if crop in CROPS else (None, None)
    if pct < DRY_BELOW:
        text = ("The soil is dry. Irrigate soon, preferably in the early morning or evening, and check the soil by hand "
                "at root depth first.")
        if need == "high":
            text += f" {name.capitalize()} needs plenty of water, so do not let the field stay dry."
        text += (" If water is scarce, crops that need little water, such as ragi, jowar, bajra, red gram and horse gram, "
                 "suit these conditions better.")
    elif pct > WET_ABOVE:
        text = ("The soil is very wet. Do not irrigate now; make sure water drains out of the field, because standing water "
                "can rot the roots and spread wilt and root diseases.")
        if crop == "rice":
            text = "The soil is very wet, which suits rice (paddy); keep the water level as advised for its stage."
    else:
        text = "The soil moisture is in a good range, so no irrigation is needed right now. Check again in a day or two."
    return text


# Water level sensor (UNO Q, A1): percent of the sensor strip under water.
WATER_BANDS = ((5, "no water"), (35, "a low water level"), (70, "a medium water level"), (101, "a high water level"))


def water_band(pct: float) -> str:
    return next(name for limit, name in WATER_BANDS if pct < limit)


def water_advice(pct: float) -> str:
    """What the water level means, wherever the sensor is placed (tank, channel or field)."""
    if pct < 5:
        return ("There is no water at the sensor. If it is in a tank or farm pond, refill it before the next "
                "irrigation; if it is in a paddy field, the field has dried out and needs water.")
    if pct < 35:
        return "The water level is low, so plan to refill or irrigate soon."
    if pct < 70:
        return "The water level is moderate."
    return ("The water level is high. If the sensor is in a field, check that water is not standing around crops "
            "other than rice, and open the drainage if needed; if it is in a tank, it is nearly full.")


def kb_markdown() -> str:
    """kb/15: the table above, as documents Gemma can retrieve."""
    lines = ["# Crop Climate Needs and Sensor-Based Advice", "",
             "Review status: general guide compiled for Smart Krishi from standard agronomy references. Varieties and "
             "crop stages differ, so check the local package of practices (UAS Bangalore, UAS Dharwad, KVK).", "",
             "## How Smart Krishi uses the temperature reading",
             "When a farmer asks for the temperature, Smart Krishi gives the air temperature measured by the farm sensor, "
             "says whether it suits the farmer's crop, and names the crops that grow best at that temperature. A "
             "temperature below a crop's lower limit slows growth and can damage young plants; above its upper limit, "
             "flowers and young fruits can drop.", "",
             "## How Smart Krishi uses the soil moisture reading",
             f"For a calibrated soil sensor, a reading below {DRY_BELOW} percent means the soil is dry and the crop should "
             f"be irrigated soon, a reading between {DRY_BELOW} and {WET_ABOVE} percent is a good range, and a reading above "
             f"{WET_ABOVE} percent means the soil is very wet, so irrigation should stop and the field should drain. These "
             "bands are a rough guide: each field has its own dry and wet readings, and the soil should always be checked "
             "by hand at root depth before irrigating.", "",
             "## How Smart Krishi uses the water level reading",
             "The water level sensor shows what percent of its sensor strip is under water. Below 5 percent there is no "
             "water at the sensor, below 35 percent the water level is low, below 70 percent it is moderate, and above "
             "that it is high. In a tank or farm pond, a low level means it should be refilled before the next "
             "irrigation; in a field, a high level means water may be standing, which only rice tolerates.", ""]
    for k, (name, cold, lo, hi, hot, need, season) in CROPS.items():
        lines += [f"## Temperature and water needs of {name}",
                  f"{name.capitalize()} grows best at an air temperature between {lo} and {hi} degrees Celsius. Below "
                  f"{cold} degrees it is too cold and above {hot} degrees it is too hot for good growth. Its water need is "
                  f"{need}. Main season in Karnataka: {season}.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    if "--kb" in sys.argv:
        out = Path(__file__).resolve().parent.parent / "kb" / "15_crop_climate_and_sensor_advice.md"
        out.write_text(kb_markdown(), encoding="utf-8")
        print("wrote", out)
