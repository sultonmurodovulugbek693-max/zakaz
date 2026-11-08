"""
O'zbekiston manzillari: viloyatlar va ulardagi tumanlar (shaharlar bilan).

Ro'yxatdan o'tishda manzil erkin matn emas — shu ro'yxatdan tanlanadi:
  • o'qituvchi dars beradigan tuman(lar)ni belgilaydi («joy ochadi»),
  • o'quvchi o'z tumanini tanlaydi va arizasi o'sha tumandagi o'qituvchilarga boradi.

Nom o'zgarsa yoki tuman qo'shilsa — faqat shu faylni tahrirlang (ilova ro'yxatni API orqali oladi).
"""

REGIONS: dict[str, list[str]] = {
    "Toshkent shahri": [
        "Bektemir", "Chilonzor", "Mirobod", "Mirzo Ulug'bek", "Olmazor", "Sergeli", "Shayxontohur",
        "Uchtepa", "Yakkasaroy", "Yangihayot", "Yashnobod", "Yunusobod",
    ],
    "Toshkent viloyati": [
        "Angren shahri", "Bekobod shahri", "Bekobod tumani", "Bo'ka", "Bo'stonliq", "Chinoz",
        "Chirchiq shahri", "Nurafshon shahri", "Ohangaron", "Olmaliq shahri", "Oqqo'rg'on", "Parkent",
        "Piskent", "Qibray", "Quyichirchiq", "Toshkent tumani", "O'rtachirchiq", "Yangiyo'l",
        "Yuqorichirchiq", "Zangiota",
    ],
    "Andijon viloyati": [
        "Andijon shahri", "Andijon tumani", "Asaka", "Baliqchi", "Bo'z", "Buloqboshi", "Izboskan",
        "Jalaquduq", "Marhamat", "Oltinko'l", "Paxtaobod", "Qo'rg'ontepa", "Shahrixon", "Ulug'nor",
        "Xo'jaobod", "Xonobod shahri",
    ],
    "Buxoro viloyati": [
        "Buxoro shahri", "Buxoro tumani", "G'ijduvon", "Jondor", "Kogon shahri", "Kogon tumani",
        "Olot", "Peshku", "Qorako'l", "Qorovulbozor", "Romitan", "Shofirkon", "Vobkent",
    ],
    "Farg'ona viloyati": [
        "Beshariq", "Bog'dod", "Buvayda", "Dang'ara", "Farg'ona shahri", "Farg'ona tumani", "Furqat",
        "Marg'ilon shahri", "Oltiariq", "Qo'qon shahri", "Qo'shtepa", "Quva", "Quvasoy shahri",
        "Rishton", "So'x", "Toshloq", "Uchko'prik", "O'zbekiston", "Yozyovon",
    ],
    "Jizzax viloyati": [
        "Arnasoy", "Baxmal", "Do'stlik", "Forish", "G'allaorol", "Jizzax shahri", "Mirzacho'l",
        "Paxtakor", "Sharof Rashidov", "Yangiobod", "Zafarobod", "Zarbdor", "Zomin",
    ],
    "Namangan viloyati": [
        "Chortoq", "Chust", "Kosonsoy", "Mingbuloq", "Namangan shahri", "Namangan tumani", "Norin",
        "Pop", "To'raqo'rg'on", "Uchqo'rg'on", "Uychi", "Yangiqo'rg'on",
    ],
    "Navoiy viloyati": [
        "Karmana", "Konimex", "Navbahor", "Navoiy shahri", "Nurota", "Qiziltepa", "Tomdi",
        "Uchquduq", "Xatirchi", "Zarafshon shahri",
    ],
    "Qashqadaryo viloyati": [
        "Chiroqchi", "Dehqonobod", "G'uzor", "Kasbi", "Kitob", "Ko'kdala", "Koson", "Mirishkor",
        "Muborak", "Nishon", "Qamashi", "Qarshi shahri", "Qarshi tumani", "Shahrisabz shahri",
        "Shahrisabz tumani", "Yakkabog'",
    ],
    "Qoraqalpog'iston Respublikasi": [
        "Amudaryo", "Beruniy", "Bo'zatov", "Chimboy", "Ellikqal'a", "Kegeyli", "Mo'ynoq", "Nukus shahri",
        "Nukus tumani", "Qanliko'l", "Qo'ng'irot", "Qorao'zak", "Shumanay", "Taxiatosh", "Taxtako'pir",
        "To'rtko'l", "Xo'jayli",
    ],
    "Samarqand viloyati": [
        "Bulung'ur", "Ishtixon", "Jomboy", "Kattaqo'rg'on shahri", "Kattaqo'rg'on tumani", "Narpay",
        "Nurobod", "Oqdaryo", "Pastdarg'om", "Paxtachi", "Payariq", "Qo'shrabot", "Samarqand shahri",
        "Samarqand tumani", "Toyloq", "Urgut",
    ],
    "Sirdaryo viloyati": [
        "Boyovut", "Guliston shahri", "Guliston tumani", "Mirzaobod", "Oqoltin", "Sardoba",
        "Sayxunobod", "Shirin shahri", "Sirdaryo tumani", "Xovos", "Yangiyer shahri",
    ],
    "Surxondaryo viloyati": [
        "Angor", "Bandixon", "Boysun", "Denov", "Jarqo'rg'on", "Muzrabot", "Oltinsoy", "Qiziriq",
        "Qumqo'rg'on", "Sariosiyo", "Sherobod", "Sho'rchi", "Termiz shahri", "Termiz tumani", "Uzun",
    ],
    "Xorazm viloyati": [
        "Bog'ot", "Gurlan", "Hazorasp", "Qo'shko'pir", "Shovot", "Tuproqqal'a", "Urganch shahri",
        "Urganch tumani", "Xiva shahri", "Xiva tumani", "Xonqa", "Yangiariq", "Yangibozor",
    ],
}


def regions() -> list[str]:
    return list(REGIONS)


def districts(region: str) -> list[str]:
    return REGIONS.get(region, [])


def valid(region: str, district: str) -> bool:
    return district in REGIONS.get(region, [])


def label(region: str, district: str) -> str:
    return f"{district}, {region}" if region and district else (district or region or "")
