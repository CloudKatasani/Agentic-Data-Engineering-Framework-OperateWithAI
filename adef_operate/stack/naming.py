"""Logical-to-physical names (adapter contract section 2, 'Naming' and 'Structure').

Logical: ``<layer>.<table>`` with medallion layers bronze, silver, gold.
Physical: ``<catalog>.<schema>.<table>`` where the catalog is ``<org_prefix>_<domain>``,
the schema is the profile's layer name, and case follows ``standards.naming_case``.
"""

from __future__ import annotations

LOGICAL_LAYERS = ["bronze", "silver", "gold", "serving"]


class Naming:
    def __init__(self, profile: dict):
        v = profile.get("variables", {})
        st = profile.get("standards", {})
        self.case = st.get("naming_case", "lower_snake")
        layers = st.get("layers") or LOGICAL_LAYERS
        self.layer_map = dict(zip(LOGICAL_LAYERS, layers))
        self.inverse = {self._c(p): l for l, p in self.layer_map.items()}
        self.catalog = self._c(f"{v.get('org_prefix', 'org')}_{v.get('domain', 'data')}")

    def _c(self, s: str) -> str:
        return s.upper() if self.case == "upper_snake" else s.lower()

    def physical(self, logical: str) -> str:
        layer, table = logical.split(".", 1)
        return f"{self.catalog}.{self._c(self.layer_map.get(layer, layer))}.{self._c(table) if table != '*' else '*'}"

    def logical(self, physical: str) -> str:
        parts = physical.split(".")
        schema, table = parts[-2], parts[-1]
        return f"{self.inverse.get(schema, schema.lower())}.{table.lower()}"

    def role(self, logical_role: str) -> str:
        return self._c(logical_role.split(":", 1)[1])

    @staticmethod
    def logical_role(native: str) -> str:
        return "role:" + native.lower()

    def pipeline(self, pipeline_id: str) -> str:
        return self._c(pipeline_id)
