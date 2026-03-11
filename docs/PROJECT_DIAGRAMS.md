# MIBSP Project Diagrams (Viva Ready)

## 1. 3-Tier Architecture

```mermaid
flowchart LR
    U[Citizen / Officer / Admin Browser]
    W[Flask Web App\nRoutes + Templates + APIs]
    D[(PostgreSQL / SQLite)]
    F[(Uploads Storage)]
    E[SMTP / Email]
    S[Twilio SMS Optional]

    U -->|HTTPS| W
    W --> D
    W --> F
    W --> E
    W --> S
```

## 2. ER Diagram (Core Entities)

```mermaid
erDiagram
    DEPARTMENT ||--o{ SERVICE : has
    DEPARTMENT ||--o{ USER : contains
    DEPARTMENT ||--o{ COMPLAINT : receives
    SERVICE ||--o{ COMPLAINT : for
    USER ||--o{ COMPLAINT : assigned_to
    USER ||--o{ AUDIT_LOG : creates

    DEPARTMENT {
      int id PK
      string name
      string description
      datetime created_at
    }
    SERVICE {
      int id PK
      string name
      int department_id FK
      int sla_days
      string description
    }
    USER {
      int id PK
      string username
      string email
      string role
      int department_id FK
      bool is_active
      int failed_login_attempts
      datetime locked_until
    }
    COMPLAINT {
      int id PK
      string tracking_id
      int service_id FK
      int department_id FK
      int assigned_to FK
      string status
      string priority
      int escalation_level
      int reopen_count
      int citizen_rating
      string ai_sentiment
      bool ai_urgent
      float location_lat
      float location_lng
      datetime sla_due_at
      datetime submitted_at
      datetime resolved_at
    }
    AUDIT_LOG {
      int id PK
      int user_id FK
      string action
      string previous_hash
      string row_hash
      datetime timestamp
    }
```

## 3. Sequence Diagram (Complaint Submission Flow)

```mermaid
sequenceDiagram
    participant Citizen
    participant Frontend
    participant Flask
    participant DB
    participant Notify as Email/SMS

    Citizen->>Frontend: Fill form + description + optional geo
    Frontend->>Flask: POST /submit
    Flask->>Flask: Validate form + classify urgency/sentiment
    Flask->>DB: Insert complaint + SLA due date
    DB-->>Flask: Commit success (tracking_id)
    Flask->>Notify: Notify internal recipients (optional)
    Flask-->>Frontend: Redirect /confirmation/<tracking_id>
    Frontend-->>Citizen: Show tracking ID
```

## 4. Threat Model (High-Level)

```mermaid
flowchart TD
    A[Threat: Brute-force login] --> B[Control: Account lock + rate limit]
    C[Threat: CSRF on forms/APIs] --> D[Control: Flask-WTF CSRF token]
    E[Threat: Tampering with logs] --> F[Control: Hash-chained audit logs]
    G[Threat: Sensitive PII leakage] --> H[Control: Anonymous complaint design]
    I[Threat: Stale cached public data] --> J[Control: no-store API + SW network-first]
```

## 5. Deployment Architecture (Render)

```mermaid
flowchart LR
    GH[GitHub Repository] -->|Auto deploy on main| R[Render Web Service]
    R --> APP[Gunicorn + Flask App]
    APP --> DB[(Render PostgreSQL)]
    APP --> FS[/tmp uploads disk]
    APP --> SMTP[SMTP Server]
    APP --> TW[Twilio Optional]
    LB[Render Edge / HTTPS] --> APP
```

