"""The inline and router pose sets stay inside the corner cat's footprint, so the text
limits beside the corner (computed from the results-screen idle poses) keep holding."""

import unittest

from flugato import pet


def poses_of(base, acts):
    names = {p for p, _, _ in base}
    for seq in acts.values():
        names |= {p for p, _, _ in seq}
    return names


def left_edges(names, mood="good"):
    edges = [pet.CW] * pet.CH
    for pose in names:
        for frame in range(4):
            for r, row in enumerate(pet.cells(mood, pose, frame)):
                filled = [i for i, ch in enumerate(row) if ch != "."]
                if filled:
                    edges[r] = min(edges[r], filled[0])
    return edges


class PetSets(unittest.TestCase):
    def test_sequences_reference_known_poses(self):
        for base, acts in ((pet.INLINE_BASE, pet.INLINE_ACTS), (pet.ROUTER_BASE, pet.ROUTER_ACTS)):
            for name in poses_of(base, acts):
                self.assertIn(name.removesuffix("_L"), pet.POSES)
            for seq in list(acts.values()) + [base]:
                for pose, ticks, dx in seq:
                    self.assertGreater(ticks, 0)
                    self.assertEqual(dx, 0)      # the corner cat does not walk

    def test_sets_stay_inside_the_idle_footprint(self):
        idle = left_edges(poses_of(pet.IDLE_BASE, pet.IDLE_ACTS))
        for base, acts in ((pet.INLINE_BASE, pet.INLINE_ACTS), (pet.ROUTER_BASE, pet.ROUTER_ACTS)):
            edges = left_edges(poses_of(base, acts))
            for r in range(4):            # the rows that cross the last results row's text
                self.assertGreaterEqual(edges[r], idle[r], f"row {r}")

    def test_every_pose_renders(self):
        for name in pet.POSES:
            rows = pet.cells("good", name)
            self.assertEqual(len(rows), pet.CH)
            self.assertTrue(all(len(r) == pet.CW for r in rows))


if __name__ == "__main__":
    unittest.main()
