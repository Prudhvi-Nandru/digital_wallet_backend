"""Small parser interface. Production LLMs may implement parse_question; they never return SQL."""
from dataclasses import dataclass
from datetime import date, timedelta
import re

@dataclass
class StatementIntent:
    kind: str | None
    counterparty: str | None
    start: date
    end_exclusive: date
    type_code: int | None = None


class StatementParser:
    TYPE_MAP = {
        'sent': 1,
        'received': 2,
        'largest': 3,
        'count': 4,
    }
    def parse_question(self, question: str) -> StatementIntent:
        if not question or not question.strip():
            return StatementIntent(None, None, date.today(), date.today() + timedelta(days=1), None)

        q = question.lower()
        today = date.today()
        if 'last month' in q:
            first = today.replace(day=1)
            start = (first - timedelta(days=1)).replace(day=1)
            end = first
        else:
            start = today.replace(day=1)
            end = today + timedelta(days=1)

        m = re.search(r'from\s+(\d{4}-\d{2}-\d{2})\s+(?:to|through|until)\s+(\d{4}-\d{2}-\d{2})', q)
        if m:
            start = date.fromisoformat(m.group(1))
            end = date.fromisoformat(m.group(2)) + timedelta(days=1)
        if any(x in q for x in ('largest', 'biggest', 'maximum')):
            kind = 'largest'
        elif any(x in q for x in ('how many', 'number of', 'count')):
            kind = 'count'
        elif any(x in q for x in ('received', 'receive', 'incoming')):
            kind = 'received'
        elif any(x in q for x in ('sent', 'send', 'paid', 'pay', 'transfer to')):
            kind = 'sent'
        else:
            kind = None

        cp = None
        m = re.search(
            r'\b(?:to|for|from)\s+([a-z][a-z .\'-]{0,50}?)(?:\s+(?:last month|this month|from\s+\d{4}-\d{2}-\d{2})|[?.!,]|$)',
            question,
            re.I,
        )
        if m:
            cp = m.group(1).strip().title()

        return StatementIntent(kind, cp, start, end, self.TYPE_MAP.get(kind) if kind else None)

    def parse_question_json(self, question: str) -> dict:
        intent = self.parse_question(question)
        payload = {
            'type': intent.type_code,
            'counterparty': intent.counterparty,
            'from': intent.start.isoformat(),
            'to': intent.end_exclusive.isoformat(),
        }
        if intent.kind is None:
            payload['type'] = None
        return payload

class MockStatementParser(StatementParser):
    """Network-free implementation used by tests; parser is intentionally deterministic."""
    pass
