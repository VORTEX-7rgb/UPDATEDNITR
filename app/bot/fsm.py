"""FSM state groups shared across bot handler modules.

Kept in a dedicated module (not inside any handler) so every feature router can
import the same StatesGroup objects without creating circular imports.
"""

from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    waiting_for_roll = State()      # Waiting for 9-char NITRIS Roll Number
    waiting_for_password = State()  # Waiting for NITRIS Password
    verifying = State()             # Currently verifying with NITRIS


class Deregistration(StatesGroup):
    waiting_for_confirm = State()   # Waiting for the user to type DELETE


class InboxSearch(StatesGroup):
    waiting_for_query = State()     # Waiting for user to send search text


class QuestionPaperFlow(StatesGroup):
    waiting_for_subject = State()       # Selecting subject from lists
    waiting_for_search_query = State()  # Waiting for search query
    waiting_for_year = State()          # Waiting for year selection
