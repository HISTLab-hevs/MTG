"""Who is behind MTG, for the About page.

Transcribed from the project site (mtg.histlab.hevs.ch), whose own content.json
is the source of record. Kept here as plain data rather than fetched at request
time: the About page must render with no network, and a research consortium
changes about once a year, not per deploy. The portraits and logos in
app/static/img/about come from the same place, resized to the sizes the page
renders them at so they stay sharp on high-density screens.
"""

TEAMS = [
    {
        "name": "HES-SO Valais-Wallis · Haute Ecole d'Ingénierie",
        "people": [
            {
                "name": "Davide Calvaresi",
                "role": "Associate Professor",
                "photo": "dc.jpg",
                "url": "https://www.linkedin.com/in/davidecalvaresi/",
            },
            {
                "name": "Pierre-André Mudry",
                "role": "Full Professor, Head of ISC",
                "photo": "pam.jpg",
                "url": "https://www.linkedin.com/in/pamudry/",
            },
            {
                "name": "Elia Pacioni",
                "role": "Research Assistant",
                "photo": "ep.jpg",
                "url": "https://www.linkedin.com/in/elia-pacioni/",
            },
            {
                "name": "Valerio Crocetti",
                "role": "Research Assistant",
                "photo": "vc.jpg",
                "url": "https://www.linkedin.com/in/valerio-crocetti-7737a1342/",
            },
        ],
    },
    {
        "name": "EHL Hospitality Business School",
        "people": [
            {
                "name": "Tatyana Tsukanova",
                "role": "Assistant Professor",
                "photo": "tt.jpg",
                "url": "https://www.linkedin.com/in/tatyana-tsukanova/",
            },
        ],
    },
    {
        "name": "Entrepreneurship and Management Institute (IEM)",
        "people": [
            {
                "name": "Natalie Sarrasin",
                "role": "Assistant Professor",
                "photo": "ns.jpg",
                "url": "https://www.linkedin.com/in/natalie-sarrasin/",
            },
        ],
    },
]

ORGANIZATIONS = [
    {
        "name": "HES-SO Valais-Wallis",
        "subtitle": "Haute Ecole d'Ingénierie",
        "logo": "hesso_logo.png",
        "url": "https://www.hevs.ch/fr",
    },
    {
        "name": "HES-SO",
        "subtitle": "EHL Hospitality Business School",
        "logo": "ehl.png",
        "url": "https://www.ehl.edu/en/about-ehl/accreditations-and-memberships",
    },
    {
        "name": "HES-SO",
        "subtitle": "Entrepreneurship and Management Institute (IEM)",
        "logo": "hesso_logo.png",
        "url": (
            "https://www.hevs.ch/en/applied-research/"
            "research-institute-entrepreneurship-management/"
        ),
    },
]


ABSTRACT = [
    "As large language models change how students write and how work is "
    "assessed, course material built on memorisation and factual recall no "
    "longer measures what it used to. Educators are largely left to respond on "
    "their own, without the frameworks or the tools to do it systematically.",
    "Mind the Gap is a co-design platform that helps educators analyse and "
    "redesign their own course material. It combines a language model's reading "
    "of a document with rule-based pedagogical reasoning grounded in frameworks "
    "such as Bloom's taxonomy and constructive alignment, then reports what it "
    "found: the parts of a course most exposed to routine automation, and "
    "higher-order alternatives that call for creativity, critical thinking and "
    "collaboration.",
]

SITE_URL = "https://mtg.histlab.hevs.ch/"

CONTACTS = [
    {
        "name": "Davide Calvaresi",
        "lines": ["Associate Professor", "HES-SO Valais-Wallis"],
        "email": "davide.calvaresi@hevs.ch",
    },
    {
        "name": "Tatyana Tsukanova",
        "lines": ["Assistant Professor", "EHL Hospitality Business School, HES-SO"],
        "email": "tatyana.tsukanova@ehl.ch",
    },
]
