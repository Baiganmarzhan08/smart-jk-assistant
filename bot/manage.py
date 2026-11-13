import argparse
import re

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bot.database import create_database_engine, database_url
from bot.models import Building, Residence, Service
from bot.residents import CATEGORIES, STATUSES, change_ticket_status, get_residence


def slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", value):
        raise argparse.ArgumentTypeError("Код ЖК должен содержать до 48 латинских букв, цифр и дефисов.")
    return value


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 2_147_483_647:
        raise argparse.ArgumentTypeError("Введите положительное целое число.")
    return number


def group_id(value: str) -> int:
    number = int(value)
    if not -(2**63) <= number < 0:
        raise argparse.ArgumentTypeError("Вставьте отрицательный идентификатор группы из команды /chatid.")
    return number


def bounded(value: str, size: int) -> str:
    value = " ".join(value.split())
    if not 1 <= len(value) <= size or "\0" in value:
        raise ValueError(f"Допустимая длина поля: от 1 до {size} символов.")
    return value


def save_residence(session: Session, args: argparse.Namespace) -> None:
    data = {"name": bounded(args.name, 120)}
    for field, size in (("address", 240), ("dispatcher_contact", 160)):
        if (value := getattr(args, field)) is not None:
            data[field] = bounded(value, size)
    session.execute(
        insert(Residence).values(slug=args.slug, **data)
        .on_conflict_do_update(index_elements=[Residence.slug], set_=data)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальные команды оператора Smart ЖК.")
    commands = parser.add_subparsers(dest="operation", required=True)
    residence = commands.add_parser("residence", help="Сохранить карточку ЖК.")
    residence.add_argument("slug", type=slug)
    residence.add_argument("--name", required=True)
    residence.add_argument("--address")
    residence.add_argument("--dispatcher-contact")
    bind = commands.add_parser("bind", help="Привязать Telegram-группу к ЖК.")
    bind.add_argument("slug", type=slug)
    bind.add_argument("chat_id", type=group_id)
    building = commands.add_parser("building", help="Сохранить корпус ЖК.")
    building.add_argument("slug", type=slug)
    building.add_argument("label")
    building.add_argument("--entrances", type=positive, required=True)
    building.add_argument("--apartments", type=positive, required=True)
    service = commands.add_parser("service", help="Сохранить услугу ЖК.")
    service.add_argument("slug", type=slug)
    service.add_argument("name")
    service.add_argument("--category", choices=CATEGORIES, required=True)
    service.add_argument("--description", required=True)
    service.add_argument("--price")
    service.add_argument("--contact")
    service.add_argument("--unavailable", action="store_true")
    status = commands.add_parser("status", help="Изменить статус заявки.")
    status.add_argument("ticket_id", type=positive)
    status.add_argument("value", choices=STATUSES)
    args = parser.parse_args()

    try:
        engine = create_database_engine(database_url())
        try:
            if args.operation == "status":
                if not change_ticket_status(engine, args.ticket_id, args.value):
                    raise ValueError("Заявка с таким номером не найдена.")
                print(f"Заявка №{args.ticket_id}: {STATUSES[args.value]}.")
                return
            with Session(engine) as session, session.begin():
                if args.operation == "residence":
                    save_residence(session, args)
                else:
                    record = get_residence(session, args.slug)
                    if args.operation == "bind":
                        record.telegram_chat_id = args.chat_id
                    elif args.operation == "building":
                        values = {"entrances": args.entrances, "apartments": args.apartments}
                        session.execute(insert(Building).values(
                            residence_id=record.id, label=bounded(args.label, 60), **values,
                        ).on_conflict_do_update(constraint="uq_buildings_residence_label", set_=values))
                    else:
                        values = {
                            "category": args.category, "description": bounded(args.description, 300),
                            "available": not args.unavailable,
                        }
                        for field, size in (("price", 80), ("contact", 120)):
                            if (value := getattr(args, field)) is not None:
                                values[field] = bounded(value, size)
                        session.execute(insert(Service).values(
                            residence_id=record.id, name=bounded(args.name, 100), **values,
                        ).on_conflict_do_update(constraint="uq_services_residence_name", set_=values))
            print("Сохранено.")
        finally:
            engine.dispose()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except SQLAlchemyError:
        raise SystemExit("Ошибка PostgreSQL. Проверьте подключение и уникальность привязки группы.") from None


if __name__ == "__main__":
    main()
