import re
from typing import List, Dict

SYSTEM_PROMPT = (
    "Siz — Telegram guruhining diqqatli kotibisiz. Muhokamalar bo'yicha qisqa xulosa chiqaring., "
    "Mavzular bo'yicha tartibla, vazifalarni, dedlaynlarni, bahsli jihatlarni va qabul qilingan qarorlarni ajratib ko'rsat. "
    "Yakunda (agar mavjud bo'lsa) Todo ro'yxatini va xavf/to'siqlar bo'limini qo'sh."
)

def chunk_messages(msgs: List[Dict], max_chars: int = 8000) -> List[str]:
    blocks = []
    current = ""
    for m in msgs:
        line = f"- @{m.get('username') or m.get('user_id')}: {m['text']}\n"
        if len(current) + len(line) > max_chars:
            if current:
                blocks.append(current)
            current = line
        else:
            current += line
    if current:
        blocks.append(current)
    return blocks

async def summarize_window(client, model: str, msgs: List[Dict], period_label: str) -> str:
    from openai import AsyncOpenAI
    try:
        aclient = AsyncOpenAI(api_key=client.api_key)
        blocks = chunk_messages(msgs)
        partial_summaries = []
        for i, block in enumerate(blocks, 1):
            resp = await aclient.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Суммируй блок {i}/{len(blocks)} {period_label}:\n{block}"}
                ]
            )
            partial_summaries.append(resp.choices[0].message.content.strip())

        final_input = "\n\n".join(f"Блок {i+1}: {s}" for i, s in enumerate(partial_summaries))
        final = await aclient.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Объедини кратко все блоки {period_label} в единый дайджест с заголовками:\n{final_input}"}
            ]
        )
        return final.choices[0].message.content.strip()
    except Exception:
        blocks = chunk_messages(msgs)
        partial_summaries = []
        for i, block in enumerate(blocks, 1):
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Суммируй блок {i}/{len(blocks)} {period_label}:\n{block}"}
                ]
            )
            partial_summaries.append(resp.choices[0].message.content.strip())
        final_input = "\n\n".join(f"Блок {i+1}: {s}" for i, s in enumerate(partial_summaries))
        final = client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Объедини кратко все блоки {period_label} в единый дайджест с заголовками:\n{final_input}"}
            ]
        )
        return final.choices[0].message.content.strip()

def build_keyword_flags(text: str, keywords_csv: str):
    if not keywords_csv:
        return []
    flags = []
    for raw in keywords_csv.split(","):
        k = raw.strip()
        if not k:
            continue
        if re.search(rf"\b{re.escape(k)}\b", text, flags=re.IGNORECASE):
            flags.append(k)
    return flags
