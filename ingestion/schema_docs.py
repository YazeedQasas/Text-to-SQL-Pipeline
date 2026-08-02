"""
Schema document definitions for the legal_db demo database.

This module is the single source of truth for what gets embedded and stored
in Qdrant for retrieval-augmented SQL generation. It mirrors db/01_schema.sql
exactly — if you change the SQL schema, update the corresponding TableDoc here.

Design notes
------------
- Granularity: every doc built here is a TABLE-level document (DOC_LEVEL_TABLE).
  Each ColumnDoc already carries its own name/type/description, so a future
  column-level retrieval mode can slice these same structures into individual
  per-column documents (DOC_LEVEL_COLUMN) without redefining the metadata —
  see ingest.py's `build_column_documents` stub.
- Description quality matters more than any other field here: the retriever
  matches a natural-language question against `description` (and to a lesser
  extent column descriptions), so descriptions should name the real-world
  entities and relationships a user would ask about, not just restate the
  column list.
"""

from dataclasses import dataclass, field

DOC_LEVEL_TABLE = "table"
DOC_LEVEL_COLUMN = "column"  # reserved for future column-level ingestion


@dataclass
class ColumnDoc:
    name: str
    type: str
    nullable: bool
    description: str


@dataclass
class ForeignKeyDoc:
    column: str
    references_table: str
    references_column: str


@dataclass
class TableDoc:
    name: str
    description: str
    columns: list[ColumnDoc]
    primary_key: str
    foreign_keys: list[ForeignKeyDoc] = field(default_factory=list)


TABLES: list[TableDoc] = [
    TableDoc(
        name="courts",
        description=(
            "Judicial bodies (courthouses) where cases are filed and heard, such as "
            "trial courts, appellate courts, and the supreme court. Use this table to "
            "answer questions about which court handled a case, a court's jurisdiction "
            "(federal vs. state), its level in the court hierarchy, or its physical "
            "location. Every case, and every judge's home court, links back here."
        ),
        primary_key="court_id",
        columns=[
            ColumnDoc("court_id", "INT", False, "Unique identifier for the court."),
            ColumnDoc("name", "VARCHAR(255)", False, "Official name of the court, e.g. 'Supreme Court'."),
            ColumnDoc("level", "ENUM('Trial','Appellate','Supreme')", False, "Position of the court in the judicial hierarchy."),
            ColumnDoc("jurisdiction", "VARCHAR(255)", False, "Governing jurisdiction of the court, e.g. 'Federal' or 'State'."),
            ColumnDoc("location", "VARCHAR(255)", False, "City or region where the court sits."),
        ],
    ),
    TableDoc(
        name="judges",
        description=(
            "Judicial officers, identified by name (full_name), who preside over "
            "hearings and issue judgements. Each judge is permanently attached to one "
            "home court. This is the table to look up whenever a question names a "
            "specific judge (e.g. 'Judge X') — resolve full_name here to judge_id, "
            "then join to hearings or judgements on judge_id to find what cases that "
            "judge presided over or ruled on. Also holds title/seniority, appointment "
            "date, and home court."
        ),
        primary_key="judge_id",
        foreign_keys=[ForeignKeyDoc("court_id", "courts", "court_id")],
        columns=[
            ColumnDoc("judge_id", "INT", False, "Unique identifier for the judge."),
            ColumnDoc("full_name", "VARCHAR(255)", False, "Judge's full name."),
            ColumnDoc("title", "VARCHAR(100)", False, "Judicial title, e.g. 'Chief Judge', 'Associate Judge', 'Chief Justice'."),
            ColumnDoc("court_id", "INT", False, "Home court of the judge. Foreign key to courts.court_id."),
            ColumnDoc("appointed_date", "DATE", True, "Date the judge was appointed to the bench."),
        ],
    ),
    TableDoc(
        name="lawyers",
        description=(
            "Legal counsel (attorneys) who represent parties in a case. Use this table "
            "to answer questions about a lawyer's identity, bar registration number, or "
            "the law firm they belong to. Which lawyer represented which party in which "
            "case is recorded in case_parties, not here — join through case_parties.lawyer_id."
        ),
        primary_key="lawyer_id",
        columns=[
            ColumnDoc("lawyer_id", "INT", False, "Unique identifier for the lawyer."),
            ColumnDoc("full_name", "VARCHAR(255)", False, "Lawyer's full name."),
            ColumnDoc("bar_number", "VARCHAR(50)", False, "Unique bar association registration number."),
            ColumnDoc("firm_name", "VARCHAR(255)", True, "Name of the law firm the lawyer works for, if any (NULL for solo practitioners)."),
        ],
    ),
    TableDoc(
        name="parties",
        description=(
            "Individuals or organizations involved in a legal case — plaintiffs, "
            "defendants, appellants, respondents, etc. A party is a person or company "
            "name and contact info only; their role and which case they were involved "
            "in is recorded in case_parties, not here. Use this table to look up who a "
            "named person or organization is, then join through case_parties to find "
            "their cases."
        ),
        primary_key="party_id",
        columns=[
            ColumnDoc("party_id", "INT", False, "Unique identifier for the party."),
            ColumnDoc("name", "VARCHAR(255)", False, "Full name of the individual or organization."),
            ColumnDoc("party_type", "ENUM('Individual','Organization')", False, "Whether the party is a person or a company/institution."),
            ColumnDoc("contact_info", "VARCHAR(255)", True, "Email or other contact information for the party."),
        ],
    ),
    TableDoc(
        name="cases",
        description=(
            "The central record for a legal case (lawsuit or criminal proceeding) "
            "filed at a court. This is usually the starting point for questions about "
            "'a case' — its case number, title, type (civil, criminal, appeal, etc.), "
            "filing date, and current status (Open, Closed, On Appeal, Dismissed). "
            "Everything else revolves around a case: its parties and their lawyers "
            "(case_parties), its scheduled sessions (hearings), its final ruling "
            "(judgements), and the laws it cites (case_legislation_citations)."
        ),
        primary_key="case_id",
        foreign_keys=[ForeignKeyDoc("court_id", "courts", "court_id")],
        columns=[
            ColumnDoc("case_id", "INT", False, "Unique identifier for the case."),
            ColumnDoc("case_number", "VARCHAR(50)", False, "Official docket/case number, e.g. 'CV-2023-0142'."),
            ColumnDoc("title", "VARCHAR(500)", False, "Case title/caption, typically 'Party A v. Party B'."),
            ColumnDoc("court_id", "INT", False, "Court where the case was filed. Foreign key to courts.court_id."),
            ColumnDoc("case_type", "VARCHAR(100)", False, "Category of case, e.g. 'Civil - Contract', 'Criminal - Fraud'."),
            ColumnDoc("filing_date", "DATE", False, "Date the case was filed with the court."),
            ColumnDoc("status", "ENUM('Open','Closed','On Appeal','Dismissed')", False, "Current status of the case."),
        ],
    ),
    TableDoc(
        name="case_parties",
        description=(
            "Junction table linking cases to the parties involved in them, recording "
            "each party's role (Plaintiff, Defendant, Appellant, Respondent, Third "
            "Party) and, optionally, which lawyer represented them for that case. Use "
            "this table to answer 'who are the plaintiffs/defendants in case X', 'which "
            "cases is party Y involved in', or 'which lawyer represented party Y in "
            "case X'. This is the join hub between cases, parties, and lawyers."
        ),
        primary_key="case_party_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("party_id", "parties", "party_id"),
            ForeignKeyDoc("lawyer_id", "lawyers", "lawyer_id"),
        ],
        columns=[
            ColumnDoc("case_party_id", "INT", False, "Unique identifier for this case/party link."),
            ColumnDoc("case_id", "INT", False, "The case the party is involved in. Foreign key to cases.case_id."),
            ColumnDoc("party_id", "INT", False, "The party involved in the case. Foreign key to parties.party_id."),
            ColumnDoc("role", "ENUM('Plaintiff','Defendant','Appellant','Respondent','Third Party')", False, "The party's role in this specific case."),
            ColumnDoc("lawyer_id", "INT", True, "Lawyer representing this party in this case, if recorded. Foreign key to lawyers.lawyer_id."),
        ],
    ),
    TableDoc(
        name="hearings",
        description=(
            "Scheduled court sessions (preliminary hearings, trials, arraignments, "
            "appellate reviews, etc.) held for a case, each presided over by one "
            "judge. Use this table to answer questions about when a case was heard, "
            "what type of hearing occurred, what happened at it (outcome_notes), or "
            "which judge presided. A case typically has multiple hearings over time; "
            "its final ruling is recorded separately in judgements."
        ),
        primary_key="hearing_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("judge_id", "judges", "judge_id"),
        ],
        columns=[
            ColumnDoc("hearing_id", "INT", False, "Unique identifier for the hearing."),
            ColumnDoc("case_id", "INT", False, "The case this hearing belongs to. Foreign key to cases.case_id."),
            ColumnDoc("judge_id", "INT", False, "Judge presiding over the hearing. Foreign key to judges.judge_id."),
            ColumnDoc("hearing_date", "DATETIME", False, "Date and time the hearing took place."),
            ColumnDoc("hearing_type", "VARCHAR(100)", False, "Type of hearing, e.g. 'Preliminary Hearing', 'Trial', 'Arraignment'."),
            ColumnDoc("outcome_notes", "TEXT", True, "Free-text notes on what happened or was decided at the hearing."),
        ],
    ),
    TableDoc(
        name="judgements",
        description=(
            "Final rulings/verdicts issued for a case by a judge — the authoritative "
            "outcome of the case. Use this table to answer 'what was the verdict/ruling "
            "in case X', 'who won', or 'what did the court decide and why' (summary). "
            "Legal principles established by a judgement are recorded separately in "
            "principles, joined via judgement_id. Not every case has a judgement yet "
            "(cases still Open or On Appeal may have none)."
        ),
        primary_key="judgement_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("judge_id", "judges", "judge_id"),
        ],
        columns=[
            ColumnDoc("judgement_id", "INT", False, "Unique identifier for the judgement."),
            ColumnDoc("case_id", "INT", False, "The case this judgement rules on. Foreign key to cases.case_id."),
            ColumnDoc("judge_id", "INT", False, "Judge who issued the judgement. Foreign key to judges.judge_id."),
            ColumnDoc("decision_date", "DATE", False, "Date the judgement was issued."),
            ColumnDoc("verdict", "VARCHAR(255)", False, "Short verdict label, e.g. 'Guilty', 'Judgement for Plaintiff'."),
            ColumnDoc("summary", "TEXT", False, "Narrative summary of the court's reasoning and decision."),
            ColumnDoc("full_text_url", "VARCHAR(500)", True, "Link to the full text of the judgement, if available."),
        ],
    ),
    TableDoc(
        name="principles",
        description=(
            "Legal principles (points of law, doctrines, or precedent-setting "
            "reasoning) established or applied in a judgement, tagged by area_of_law "
            "(e.g. Contract Law, Criminal Law). Use this table to answer 'what legal "
            "principle did this case establish' or 'find cases/judgements that "
            "established principles about X area of law'. Always joined back to a "
            "single judgement via judgement_id, and from there to its case."
        ),
        primary_key="principle_id",
        foreign_keys=[ForeignKeyDoc("judgement_id", "judgements", "judgement_id")],
        columns=[
            ColumnDoc("principle_id", "INT", False, "Unique identifier for the principle."),
            ColumnDoc("judgement_id", "INT", False, "The judgement that established or applied this principle. Foreign key to judgements.judgement_id."),
            ColumnDoc("title", "VARCHAR(255)", False, "Short name of the principle, e.g. 'Materiality of Breach'."),
            ColumnDoc("description", "TEXT", False, "Explanation of the legal principle and its reasoning."),
            ColumnDoc("area_of_law", "VARCHAR(100)", False, "Legal domain the principle belongs to, e.g. 'Contract Law', 'Criminal Law'."),
        ],
    ),
    TableDoc(
        name="legislations",
        description=(
            "Statutes and acts (written laws) that can be cited by cases, e.g. the "
            "'Financial Conduct Act' or 'Commercial Contracts Act'. Use this table to "
            "answer questions about a specific law's title, jurisdiction, enactment "
            "date, or current status (In Force, Repealed, Amended). To find which "
            "cases cited a piece of legislation, join through "
            "case_legislation_citations."
        ),
        primary_key="legislation_id",
        columns=[
            ColumnDoc("legislation_id", "INT", False, "Unique identifier for the legislation."),
            ColumnDoc("title", "VARCHAR(255)", False, "Official title of the statute or act."),
            ColumnDoc("jurisdiction", "VARCHAR(255)", False, "Jurisdiction the legislation applies to, e.g. 'Federal', 'State'."),
            ColumnDoc("enactment_date", "DATE", False, "Date the legislation was enacted."),
            ColumnDoc("status", "ENUM('In Force','Repealed','Amended')", False, "Current legal status of the legislation."),
        ],
    ),
    TableDoc(
        name="case_legislation_citations",
        description=(
            "Junction table linking cases to the legislations (statutes) they cite "
            "during proceedings, including the specific article/section cited and the "
            "context of the citation. Use this table to answer 'which laws did case X "
            "cite', 'which cases cited legislation Y', or 'which section of a statute "
            "was cited and why'. This is the join hub between cases and legislations."
        ),
        primary_key="citation_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("legislation_id", "legislations", "legislation_id"),
        ],
        columns=[
            ColumnDoc("citation_id", "INT", False, "Unique identifier for the citation record."),
            ColumnDoc("case_id", "INT", False, "The case making the citation. Foreign key to cases.case_id."),
            ColumnDoc("legislation_id", "INT", False, "The legislation being cited. Foreign key to legislations.legislation_id."),
            ColumnDoc("article_section", "VARCHAR(100)", True, "Specific article or section of the legislation cited, e.g. 'Section 12'."),
            ColumnDoc("citation_context", "TEXT", True, "Explanation of why/how the legislation was cited in the case."),
        ],
    ),
]


def build_table_doc_text(table: TableDoc) -> str:
    """Render a TableDoc into the retrieval-optimized text blob that gets embedded.

    Layout: description first (it carries the most retrieval signal), then a
    structured column list, primary key, and foreign keys the LLM needs to
    write correct JOINs.
    """
    lines = [
        f"Table: {table.name}",
        f"Description: {table.description}",
        "Columns:",
    ]
    for col in table.columns:
        nullability = "NULL" if col.nullable else "NOT NULL"
        lines.append(f"  - {col.name} ({col.type}, {nullability}): {col.description}")

    lines.append(f"Primary key: {table.primary_key}")

    if table.foreign_keys:
        lines.append("Foreign keys:")
        for fk in table.foreign_keys:
            lines.append(f"  - {table.name}.{fk.column} -> {fk.references_table}.{fk.references_column}")
    else:
        lines.append("Foreign keys: none")

    return "\n".join(lines)
