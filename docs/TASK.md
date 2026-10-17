# The Last Seat - Backend Development

Source: promocycle-tasks.pdf, CodeChef-VIT Promotional Cycle 2026-27, PDF pages 7-8. The original PDF is in the parent workspace, outside this standalone repository.

Status: implemented; see the project README and verification results.

AI tools/libraries are allowed. Submit source and setup/features/limitations/approach documentation; understand and be prepared to explain the code.

## Task sheet context

Task: The Last Seat
Problem Statement
Your college is hosting a concert with 500 seats, but 10,000 students are trying to book tickets.
The booking portal opens at 6 PM. Thousands of students attempt to reserve seats simultaneously. Some
payments fail, some users abandon their bookings, and multiple users may try to reserve the same seat.
Your task is to build the backend of a ticket booking platform that handles concurrent reservations while
ensuring that no seat is booked more than once.

A user should be able to temporarily reserve a seat, but the reservation must expire if the booking is not
confirmed within a specified period.
Expected Functionality
01   Allow users to view available seats, reserve seats, and confirm bookings.
02   Prevent multiple users from successfully booking the same seat.
03   Implement temporary reservations that automatically expire.
04   Support booking cancellations and seat reallocation.
05   Simulate payment processing with successful, failed, and delayed transactions.
06   Maintain booking consistency when requests are repeated or received concurrently.
07   Provide APIs for retrieving booking history and reservation status.
Suggested Tech Stack
•   Backend: Go, Java, Python, Rust, or any backend framework of your choice, preferably not JavaScript.
•   Database: PostgreSQL or MariaDB.
•   Containerization: Docker.
•   API: REST or gRPC.
•   Load Testing: k6, Locust, or equivalent.
Required Deliverables
•   Working backend with documented API endpoints.
•   ERD and booking workflow diagrams.
•   Docker configuration.
•   README with deployment instructions and example API requests.
•   Documentation explaining concurrency handling and how the application could scale.
•   Basic load-testing results demonstrating concurrent booking behavior.
Bonus
•   Implement Redis for caching or reservation management.
•   Introduce a waiting queue when the system reaches capacity.
•   Use RabbitMQ, Kafka, or NATS for asynchronous processing.
•   Implement idempotency keys for payment and booking requests.
•   Configure a reverse proxy.
•   Deploy the application.
