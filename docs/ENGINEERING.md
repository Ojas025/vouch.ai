# Engineering Decisions & Concepts

1.Transactional Outbox
- Atomically update the database and send messages to a message broker
- It is undesirable to couple the two services, 2PC (Two-Phase Commit) is out.
- Use a separate MessageRelay service to send messages to the message broker after db transaction
- Insert into an OUTBOX table
- Message consumer must be idempotent
