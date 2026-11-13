from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from bot.models import Building, Residence, Service, Ticket, User


DEMO_SLUG = "smart-residence"
CATEGORIES = {
    "lift": "Лифт", "electricity": "Электричество", "water": "Водоснабжение",
    "heating": "Отопление", "cleaning": "Уборка", "parking": "Парковка",
    "lighting": "Освещение", "yard": "Дворовая территория", "other": "Другое",
}
STATUSES = {"new": "Новая", "in_progress": "В работе", "done": "Выполнена", "rejected": "Отклонена"}


class ResidentInputError(ValueError):
    """The requested operation conflicts with the resident or building configuration."""


@dataclass(frozen=True)
class ResidenceProfile:
    residence: Residence
    buildings: list[Building]
    services: list[Service]

    def facts(self, *, kazakh: bool = False) -> str:
        residence = self.residence
        demo_label = " (оқу жобасындағы ТК)" if kazakh else " (учебный ЖК)"
        lines = [residence.name + (demo_label if residence.is_demo else "")]
        if kazakh:
            missing = "көрсетілмеген"
            lines.extend([
                f"Мекенжайы: {residence.address or missing}",
                f"Диспетчерлік қызмет: {residence.dispatcher_contact or missing}",
            ])
            count_labels = ("Кіреберіс саны", "Пәтер саны")
        else:
            missing = "не указано"
            lines.extend([
                f"Адрес: {residence.address or missing}",
                f"Диспетчерская: {residence.dispatcher_contact or missing}",
            ])
            count_labels = ("Подъездов всего", "Квартир всего")
        if self.buildings:
            count_label = "Анықтамалықтағы корпус саны" if kazakh else "Корпусов в справочнике"
            lines.append(f"{count_label}: {len(self.buildings)}")
            for label, field in zip(count_labels, ("entrances", "apartments"), strict=True):
                counts = [getattr(building, field) for building in self.buildings]
                if all(count is not None for count in counts):
                    lines.append(f"{label}: {sum(counts)}")
            for building in self.buildings:
                entrances = building.entrances or missing
                apartments = building.apartments or missing
                if kazakh:
                    lines.append(f"• {building.label} корпусы: кіреберіс саны — {entrances}, пәтер саны — {apartments}")
                else:
                    lines.append(f"• Корпус {building.label}: подъездов {entrances}, квартир {apartments}")
        return "\n".join(lines)

    def catalog_lines(self) -> list[str]:
        return [
            f"• {service.name} — {service.description} Цена: {service.price or 'уточняется'}."
            + (f" Контакт: {service.contact}" if service.contact else "")
            for service in self.services
        ]


def get_residence(session: Session, slug: str) -> Residence:
    residence = session.scalar(select(Residence).where(Residence.slug == slug))
    if residence is None:
        raise ResidentInputError("ЖК с таким кодом не найден.")
    return residence


def select_residence(engine: Engine, telegram_id: int, slug: str) -> str:
    with Session(engine) as session, session.begin():
        residence = get_residence(session, slug)
        session.execute(
            insert(User).values(telegram_id=telegram_id, residence_id=residence.id)
            .on_conflict_do_update(index_elements=[User.telegram_id], set_={"residence_id": residence.id})
        )
        return residence.name


def residence_profile(
    engine: Engine, *, telegram_id: int | None = None, group_chat_id: int | None = None,
) -> ResidenceProfile:
    with Session(engine) as session:
        residence = None
        if group_chat_id is not None:
            residence = session.scalar(select(Residence).where(Residence.telegram_chat_id == group_chat_id))
        elif telegram_id is not None:
            residence = session.scalar(select(Residence).join(User, User.residence_id == Residence.id).where(
                User.telegram_id == telegram_id
            ))
        if residence is None:
            residence = get_residence(session, DEMO_SLUG)
        buildings = list(session.scalars(
            select(Building).where(Building.residence_id == residence.id).order_by(Building.id)
        ))
        services = list(session.scalars(
            select(Service).where(Service.residence_id == residence.id, Service.available.is_(True)).order_by(Service.id)
        ))
        return ResidenceProfile(residence, buildings, services)


def create_ticket(
    engine: Engine, *, submission_key: UUID, telegram_id: int, residence_id: int,
    building_id: int, category: str, entrance: int, description: str,
) -> int:
    description = description.strip()
    if category not in CATEGORIES or not 1 <= len(description) <= 1000 or "\0" in description:
        raise ResidentInputError("Проверьте категорию и описание заявки.")
    if not 1 <= entrance <= 2_147_483_647:
        raise ResidentInputError("Введите корректный номер подъезда.")
    with Session(engine) as session, session.begin():
        existing = session.scalar(select(Ticket).where(Ticket.submission_key == submission_key))
        if existing is not None:
            if existing.telegram_id != telegram_id:
                raise ResidentInputError("Заявка не найдена.")
            return existing.id
        building = session.scalar(select(Building).where(
            Building.id == building_id, Building.residence_id == residence_id,
        ))
        if building is None or (building.entrances is not None and entrance > building.entrances):
            raise ResidentInputError("Проверьте корпус и номер подъезда. Начните заново через /request.")
        session.execute(insert(User).values(telegram_id=telegram_id).on_conflict_do_nothing())
        ticket_id = session.scalar(
            insert(Ticket).values(
                submission_key=submission_key, telegram_id=telegram_id, residence_id=residence_id,
                building_id=building_id, category=category, entrance=entrance, description=description,
            ).on_conflict_do_nothing(index_elements=[Ticket.submission_key]).returning(Ticket.id)
        )
        if ticket_id is not None:
            return ticket_id
        existing_id = session.scalar(select(Ticket.id).where(
            Ticket.submission_key == submission_key, Ticket.telegram_id == telegram_id,
        ))
        if existing_id is None:
            raise ResidentInputError("Заявка не найдена.")
        return existing_id


def own_tickets(engine: Engine, telegram_id: int, ticket_id: int | None = None) -> list[Ticket]:
    query = select(Ticket).where(Ticket.telegram_id == telegram_id)
    if ticket_id is not None:
        query = query.where(Ticket.id == ticket_id)
    with Session(engine) as session:
        return list(session.scalars(query.order_by(Ticket.id.desc()).limit(5)))


def change_ticket_status(engine: Engine, ticket_id: int, status: str) -> bool:
    if status not in STATUSES:
        raise ResidentInputError("Неизвестный статус.")
    with Session(engine) as session, session.begin():
        return session.scalar(update(Ticket).where(Ticket.id == ticket_id).values(status=status).returning(Ticket.id)) is not None
