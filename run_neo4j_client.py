from neo4j import GraphDatabase

uri = "bolt://localhost:7687"  # Replace with your Neo4j instance URI
username = "neo4j"
password = "password"

driver = GraphDatabase.driver(uri, auth=(username, password))

driver.verify_connectivity()

x = driver.execute_query("""
    RETURN 1
    """
)

print(x)

