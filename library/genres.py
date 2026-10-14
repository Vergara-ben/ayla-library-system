"""The library's standard genres, and which one a sheet's own wording belongs to."""

import re

STANDARD_GENRES = [
    # Fiction
    'Thriller & Suspense', 'Mystery', 'Romance', 'Horror', 'Fantasy & Science Fiction', 'Historical Fiction',
    'Fiction', 'Classics', 'Philippine Literature', 'Young Adult',
    "Children's Picture Books", "Children's Fiction", "Children's Non-Fiction", 'Activity Books',
    # Reference
    'Encyclopedias', 'Dictionaries', 'General Reference',
    # School subjects
    'Education', 'English Language & Literature', 'Filipino Language & Literature', 'Mathematics', 'Science',
    'Social Studies', 'Values Education', 'Religion', 'MAPEH', 'Technology & Livelihood',
    'Computers & Technology', 'Health & Fitness',
    # College and general non-fiction
    'Accounting', 'Business', 'Economics', 'Finance & Banking', 'Management', 'Marketing & Sales', 'Statistics',
    'Medicine & Nursing', 'History', 'Biography & Memoir', 'Social Sciences', 'Philosophy', 'Arts & Music',
    'Cookbooks & Food', 'Self-Help & Careers', 'True Crime',
]

# Tried in order; the first match wins. Built from every genre the catalogue held on 2026-10-14.
RULES = [
    # Single titles the broader rules below would misfile.
    ('Biography & Memoir', r'biography / young adult'),
    ('Marketing & Sales', r'marketing readings'),
    ('Accounting', r'auditing & information|accounting software'),
    ('Social Sciences', r'political science'),
    ('Medicine & Nursing', r'medical science'),
    # Reference.
    ('Encyclopedias', r'encyclopedia'),
    ('Dictionaries', r'dictionar'),
    ('General Reference', r'general reference|academic catalog|^reference /|business reference|exam reviewer'),
    # Children's before the fiction words.
    ("Children's Picture Books", r"picture book|pop-up|storybook|story collection|children's holiday"),
    ('Cookbooks & Food', r'cookbook for kids'),
    ("Children's Fiction", r"children|easy reader|mystery / juvenile"),
    ('Activity Books', r'^activity|educational / activity|coloring|penmanship'),
    ('Young Adult', r'young adult|/ ya$|^dystopian'),
    ('Philippine Literature', r'philippine literature|classic philippine'),
    ('Filipino Language & Literature', r'filipino|academic literature / poetry|wika|philippine linguistics'),
    ('Classics', r'^classic|study guide / play'),
    ('True Crime', r'true crime'),
    ('Biography & Memoir', r'biograph|memoir|autobiograph'),
    ('Mystery', r'^mystery|suspense / mystery|thriller / mystery'),
    ('Thriller & Suspense', r'thriller|suspense'),
    ('Romance', r'romance|chick lit'),
    ('Horror', r'horror|gothic'),
    ('Fantasy & Science Fiction', r'fantasy|science fiction|sci-fi'),
    ('Historical Fiction', r'historical fiction'),
    ('Fiction', r'fiction|anthology|satire|^novel'),
    # School subjects.
    ('Medicine & Nursing', r'nursing'),
    ('Values Education', r'values|character building'),
    ('Religion', r'religio|bible|scripture'),
    ('Social Studies', r'social studies|hekasi|sibika|araling'),
    ('English Language & Literature', r'english|language & grammar|reading|writing|phonics|linguistics|speech|'
                                      r'educational / language|textbook / (language|literature)|french'),
    ('MAPEH', r'mapeh|arts & physical education'),
    ('Health & Fitness', r'physical education|fitness|wellness|yoga|diet & nutrition'),
    ('Cookbooks & Food', r'cook|culinary|baking'),
    ('Technology & Livelihood', r'home economics|technical-vocational|livelihood'),
    ('Computers & Technology', r'computer|information (technology|systems)|software|educational / technology'),
    ('Mathematics', r'math|algebra|calculus'),
    ('Statistics', r'statistic|quantitative|management science|operations research'),
    ('Arts & Music', r'(^|\W)art(\W|$)|art history|music|dance|crafts|interior design|photograph|exhibition'),
    ('Economics', r'health economics'),
    ('Management', r'corporate health'),
    ('Social Sciences', r'ufology|social science'),
    ('Science', r'(?<!social )science|biology|chemistry|physics|forensic'),
    ('Medicine & Nursing', r'medic|trauma|pharmacolog|health'),
    ('Accounting', r'accounting|auditing|cpa review|taxation'),
    ('Marketing & Sales', r'marketing|sales|advertising|retailing|public relations|selling'),
    ('Finance & Banking', r'financ|banking|invest|insurance|credit|money'),
    ('Economics', r'econom'),
    ('Management', r'management|managerial|organizational|strategic|leadership|personnel|human resource|'
                   r'labor relations|hospitality|logistics|consulting|business policy|business administration|'
                   r'office'),
    ('Business', r'business|entrepreneur|customer|fundraising|real estate'),
    ('History', r'history|historiograph|geograph|western civilization|archaeolog|asian studies'),
    ('Philosophy', r'philosoph|logic'),
    ('Social Sciences', r'psycholog|sociolog|political|politics|public (administration|policy)|'
                        r'international relations|social work|urban|criminal justice|anthropolog|'
                        r'mass communication|gender'),
    ('Self-Help & Careers', r'self-help|career|inspiration'),
    ('Education', r'educat|textbook|teacher'),
]
_COMPILED = [(name, re.compile(rx, re.I)) for name, rx in RULES]
_CHILDRENS_NON_FICTION = re.compile(r"children's (non-fiction|educational)|juvenile biography", re.I)
_BY_LOWER = {g.lower(): g for g in STANDARD_GENRES}


def standard_genre(text):
    """'Legal Thriller' -> 'Thriller & Suspense'; None when no rule fits."""
    text = ' '.join(str(text or '').split())
    if not text:
        return None
    if text.lower() in _BY_LOWER:
        return _BY_LOWER[text.lower()]
    if _CHILDRENS_NON_FICTION.search(text):
        return "Children's Non-Fiction"
    # "Non-Fiction / History" is history, not fiction.
    text = re.sub(r'non-?fiction', ' ', text, flags=re.I)
    for name, rx in _COMPILED:
        if rx.search(text):
            return name
    return None
