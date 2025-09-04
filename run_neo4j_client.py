from neo4j import GraphDatabase

uri = "bolt://localhost:7687"  # Replace with your Neo4j instance URI
username = "neo4j"
password = "password"

driver = GraphDatabase.driver(uri, auth=(username, password))

driver.verify_connectivity()


with driver.session() as session:
    try:
        result = session.run("""
            RETURN 2
            """
        )

        for record in result:
            print(record)
    except Exception as e:
        print(e.message)

    try:
        result = session.run("""
            RETURN 1
            """
        )

        for record in result:
            print(record)
    except Exception as e:
        print(e.message)
