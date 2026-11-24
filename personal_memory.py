"""Personal memories. Only accepted facts reach disk; suggestions live for one turn."""
import fcntl
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import date

from memory_extraction import FIELDS, KINDS, extract
from user_settings import MEMORY_FILE, write_private_json


def normalized(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c))))


STOP = set("""a an and are as at be by can do does for from how i in is it me my of on or that the
this to what with would you your about prefer like usually use please tell know remember
aos as com como da das de do dos e em eu me meu meus minha minhas na nas no nos o os
para por qual que se sobre um uma voce gosto prefiro costumo""".split())


def words(text):
    return {word for word in normalized(text).split() if len(word) > 1 and word not in STOP}


@contextmanager
def memory_lock():
    path = MEMORY_FILE.with_suffix(".lock")
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def records():
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    result = []
    if isinstance(data, list):
        # Preserve legacy facts as notes. Their original speaker cannot be inferred.
        for index, item in enumerate(data, 1):
            if not isinstance(item, dict) or not isinstance(item.get("fato"), str):
                raise ValueError("Invalid legacy memory")
            result.append({"id": index, "category": "fact", "source": "explicit",
                           "topics": [], "data": "", **item})
            if result[-1]["category"] not in ("preference", "routine", "project"):
                result[-1]["category"] = "fact"
    else:
        if not isinstance(data, dict) or set(data) != {"version", "person", "memories"} or data["version"] != 2:
            raise ValueError("Unknown memory schema")
        person = data["person"]
        if not isinstance(person, dict) or set(person) != FIELDS or not isinstance(data["memories"], list):
            raise ValueError("Invalid personal memory structure")
        entries = [(field, entry) for field, entry in person.items() if entry is not None]
        for entry in data["memories"]:
            if not isinstance(entry, dict) or entry.get("category") not in ("preference", "project", "routine", "fact"):
                raise ValueError("Invalid memory category")
            entries.append((entry["category"], {k: v for k, v in entry.items() if k != "category"}))
        for category, entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "value", "source", "date", "topics"}:
                raise ValueError("Invalid memory record")
            result.append(dict(id=entry["id"], fato=entry["value"], category=category,
                               data=entry["date"], source=entry["source"], topics=entry["topics"]))
    ids = [entry["id"] for entry in result]
    if (not all(type(value) is int and value > 0 for value in ids)
            or len(set(ids)) != len(ids)
            or not all(isinstance(entry["topics"], list) and
                       all(isinstance(t, str) for t in entry["topics"]) for entry in result)):
        raise ValueError("Invalid memory metadata")
    for entry in result:
        if (not isinstance(entry["fato"], str) or not entry["fato"].strip()
                or entry["source"] not in ("explicit", "corrected", "confirmed", "automatic")
                or not isinstance(entry["data"], str)):
            raise ValueError("Invalid memory metadata")
    return sorted(result, key=lambda entry: entry["id"])


def write_records(items):
    person = {field: None for field in sorted(FIELDS)}
    memories = []
    for item in items:
        entry = dict(id=item["id"], value=item["fato"], topics=item["topics"],
                     date=item["data"], source=item["source"])
        category = item["category"]
        if category in FIELDS:
            person[category] = entry
        else:
            memories.append(dict(entry, category=category if category in KINDS else "fact"))
    write_private_json(MEMORY_FILE, {"version": 2, "person": person, "memories": memories})


def describe(item):
    if item["category"] in FIELDS:
        return f'{item["category"]}: {item["fato"]}'
    return item["fato"]


# Also match common topics for explicit and legacy facts that have no model tags.
TOPICS = (
    words("food meals recipes dinner lunch eat diet vegetarian vegan comida refeições receitas jantar almoço comer dieta vegetariano vegetariana vegano vegana"),
    words("music instruments música instrumentos piano cello violin violino violoncelo guitarra guitar jazz"),
    words("reply replies answers responses resposta respostas responder explicação explicações"),
    words("programming coding software código programação programar"),
)


def relevant(query, limit=3):
    terms = words(query)
    matches = []
    labels = {"name": "name nome chamo chama", "city": "city cidade live moro mora residência",
              "school": "school escola college universidade estudo estuda estudar institution"}
    for item in records():
        fact_terms = words(item["fato"])
        topic_terms = words(" ".join(item["topics"]) + " " + labels.get(item["category"], ""))
        score = 3 * len(terms & fact_terms) + 2 * len(terms & topic_terms)
        score += sum(1 for topic in TOPICS if terms & topic and (fact_terms | topic_terms) & topic)
        if score:
            matches.append((score, item["id"], describe(item)))
    return [fact for _, _, fact in sorted(matches, reverse=True)[:limit]]


SECRET = re.compile(r"\b(password|passphrase|senha|pin|api[ _-]key|chave de api|access[ _-]token|token de acesso|private key|credit card|cart[aã]o de cr[eé]dito)\b|\b(?:sk-|gsk_)[\w-]{10,}", re.I)


def persist(candidates, source, limit):
    """Apply a complete turn atomically, retaining IDs when a profile field changes."""
    with memory_lock():
        items = records()
        original_count = len(items)
        next_id = max((item["id"] for item in items), default=0) + 1
        changed = []
        for candidate in candidates:
            field = candidate["category"]
            old = next((i for i in items if i["category"] == field and
                        (field in FIELDS or normalized(i["fato"]) == normalized(candidate["fato"]))), None)
            if old and normalized(old["fato"]) == normalized(candidate["fato"]):
                continue
            if old is None:
                old = {"id": next_id}
                next_id += 1
                items.append(old)
            old.update(candidate, data=date.today().isoformat(), source=source)
            changed.append(describe(old))
        if len(items) > max(original_count, limit):
            return "My personal memory is full. List your memories and remove one first."
        if not changed:
            return None
        write_records(items)
        return "Saved to personal memory: " + "; ".join(changed) + "."


def select(items, target):
    key = normalized(target)
    number_text = re.sub(r"^(?:(?:memory|memoria|lembranca) )?(?:number |numero )?", "", key)
    spoken = {"one": 1, "um": 1, "uma": 1, "two": 2, "dois": 2, "duas": 2,
              "three": 3, "tres": 3, "four": 4, "quatro": 4, "five": 5, "cinco": 5}
    number = int(number_text) if re.fullmatch(r"[0-9]{1,9}", number_text) else spoken.get(number_text)
    if number is not None:
        return [item for item in items if item["id"] == number]
    return [item for item in items if key and (
        key in normalized(describe(item)) or
        (item["category"] in FIELDS and f" {normalized(item['fato'])} " in f" {key} ")
    )]


LIST = re.compile(r"(?:list|show)(?: me)? (?:my |all |your )?(?:saved )?memories|what do you (?:know|remember) about me|what do you remember|(?:liste|listar|mostre)(?: as)? (?:minhas |suas )?(?:memorias|lembrancas)|o que (?:voce )?(?:lembra de mim|sabe sobre mim)|quais (?:sao )?(?:as )?minhas (?:memorias|lembrancas)")
SAVE = re.compile(r"(?:remember that |memorize (?:that )?|(?:save|store|note) (?:this|that)\s*[: ]\s*|lembre(?:-se)? (?:de que |que )?|(?:memorize|guarde|salve) que )(.+)", re.I)
DELETE = re.compile(r"(?:forget(?: that)?|delete (?:my )?memory|remove memory|esque[çc]a(?: que)?|(?:apague|apagar|remova) (?:a )?(?:mem[oó]ria|lembran[çc]a)) (.+)", re.I)
EDIT = re.compile(r"(?:correct|update|change|corrija|corrigir|atualize|altere) (?:a |my )?(?:memory|mem[oó]ria|lembran[çc]a) (.+?) (?:to|para) (.+)", re.I)


class MemoryConversation:
    def __init__(self, model, mode="auto", limit=40):
        self.model = model
        self.mode = mode if mode in {"auto", "ask"} else "manual"
        self.limit = limit
        self.pending = None
        self.changed_existing = False

    def command(self, text, history=()):
        """Consume only user-directed memory commands or an immediate confirmation."""
        text = text.strip().rstrip(".!?").strip()
        key = normalized(text)
        pending, self.pending = self.pending, None
        self.changed_existing = False
        try:
            if pending and key in {"yes", "save it", "yes save it", "sim", "pode salvar", "salve"}:
                return persist(pending, "confirmed", self.limit) or "I already remember that."
            if pending and key in {"no", "no thanks", "nao", "nao salve", "cancel"}:
                return "I discarded the suggestion."
            if LIST.fullmatch(key):
                items = records()
                return " ".join(f'{i["id"]}. {describe(i)}.' for i in items) or "No saved personal memories."
            edit = EDIT.fullmatch(text)
            delete = DELETE.fullmatch(text)
            if edit or delete:
                match = edit or delete
                found = select(records(), match[1])
                if len(found) != 1:
                    return "Please list your memories and use the number of the one you mean."
                original = found[0]
                if edit:
                    fact = edit[2].strip()
                    if SECRET.search(fact):
                        return "I don't store credentials in personal memory."
                    replacement = self.explicit(fact, [*history,
                        {"role": "assistant", "content": f"What is the corrected value for your {original['category']}?"}
                    ])
                    if len(replacement) != 1:
                        return "Please correct one memory at a time."
                    update = replacement[0]
                    if original["category"] in FIELDS and update["category"] != original["category"]:
                        return "Please state the corrected fact in full, including what it describes."
                # The model call above can take seconds. Recheck the selected
                # record under lock instead of blocking all writers during it.
                with memory_lock():
                    items = records()
                    if original not in items:
                        return "That memory changed. Please list your memories and try again."
                    if edit:
                        if update["category"] in FIELDS and any(
                            other != original and other["category"] == update["category"] for other in items
                        ):
                            return "That profile field already exists. Correct its number instead."
                        item = items[items.index(original)]
                        item.update(update, source="corrected", data=date.today().isoformat())
                    else:
                        items.remove(original)
                    write_records(items)
                self.changed_existing = True
                return "Memory updated." if edit else "Memory deleted."
            save = SAVE.fullmatch(text)
            if save:
                if SECRET.search(save[1]):
                    return "I don't store credentials in personal memory."
                return persist(self.explicit(save[1], history), "explicit", self.limit) or "I already remember that."
        except (OSError, ValueError):
            return "I couldn't access the personal memory file."

    def explicit(self, text, history=()):
        # An explicit request may legitimately be a note about something other
        # than the speaker. Never promote an unverified note into a profile field.
        return extract(text, history, self.model) or [{"fato": text, "category": "fact", "topics": []}]

    def explicit_address(self, text, history):
        candidates = [c for c in extract(text, history, self.model) if c["category"] == "form_of_address"]
        if candidates:
            try:
                return persist(candidates, "explicit", self.limit) or "I already remember that."
            except (OSError, ValueError):
                return "I couldn't access the personal memory file."

    def observe(self, text, history=()):
        if self.mode == "manual" or not text.strip() or SECRET.search(text):
            return None
        try:
            candidates = extract(text, history, self.model)
            if not candidates:
                return None
            if self.mode == "auto":
                return persist(candidates, "automatic", self.limit)
            known = {(i["category"], normalized(i["fato"])) for i in records()}
            self.pending = [c for c in candidates if (c["category"], normalized(c["fato"])) not in known]
            if self.pending:
                facts = "; ".join(describe(c) for c in self.pending)
                return f'Should I remember "{facts}"? Say "save it" or "no".'
        except (ValueError, OSError):
            return "I couldn't access the personal memory file."
