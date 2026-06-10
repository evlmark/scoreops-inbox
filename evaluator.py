import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Используем DeepSeek для оценщика — не конкурирует с Gemini за rate limit
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

MODEL = "deepseek-chat"

EVALUATOR_SYSTEM = """You are an expert quality evaluator for Plata Bank support agents.

Your task: evaluate the agent's response to a customer question, using the official knowledge base.

CRITERIA (each scored 1–10):
1. precision    — Is the information factually correct per the knowledge base?
2. language     — Did the agent respond in Spanish? (customers always write in Spanish)
3. protocol     — Did the agent follow the correct script/flow for the scenario?
4. completeness — Did the agent address everything the customer asked?
5. empathy      — Was the agent empathetic and professional?

final score = average of the 5 criteria, rounded to nearest integer.

Also identify the most relevant knowledge base article that the agent's answer should be based on.
The available articles are:
- PyME - Apartado.pdf
- PyME - Block access on all devices.pdf
- PyME - Cierre de Sesión en Todos los Dispositivos.pdf
- PyME - Congelar y Descongelar Tarjetas.pdf
- PyME - Descripción general del PyME.pdf
- PyME - Flujo de operaciones en CRM.pdf
- PyME - Planes y paquetes en CRM.pdf
- PyME - Push Call – How to Use the Form.pdf
- PyME - Solicitud del producto PFAE.pdf
- PyME - Tarifas PFAE.pdf

Respond ONLY with valid JSON, no markdown, no extra text:
{"score": <1-10>, "breakdown": {"precision": <1-10>, "language": <1-10>, "protocol": <1-10>, "completeness": <1-10>, "empathy": <1-10>}, "explanation": "<max 2 sentences in English>", "critical_error": <true|false>, "source_article": "<exact filename from the list above, or null if not applicable>"}

critical_error = true ONLY if the agent gave FACTUALLY INCORRECT information (e.g. told a rejected applicant their account is approved).

IMPORTANT FACTS — do NOT penalize agents for these:
- "Banco Plata" or "BANCO PLATA" is the correct official name. Plata has obtained a banking license and is now legally a bank. Agents are correct to refer to it as "Banco Plata". Do NOT reduce precision score for this.
- The product is officially "Cuenta PFAE" (Persona Física con Actividad Empresarial).
- Agents may use "PyME" and "PFAE" interchangeably when referring to the business account product."""


class Evaluator:
    def __init__(self, knowledge_base: str):
        self.knowledge_base = knowledge_base

    def evaluate(self, customer_question: str, agent_reply: str) -> dict:
        prompt = f"""BASE DE CONOCIMIENTO OFICIAL:
{self.knowledge_base}

---
PREGUNTA DEL CLIENTE:
{customer_question}

RESPUESTA DEL AGENTE:
{agent_reply}

Evalúa la respuesta. Responde solo con JSON."""

        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EVALUATOR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            response_format={"type": "json_object"},
        )

        text = response.choices[0].message.content.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        return json.loads(text)
