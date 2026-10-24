# Watt instruments offered to `cadmind-judge/1.2b-tools`

One line per `measure` op: its first sentence, its parameters and one example, from Watt's instrument reference.
`cadmind_einstein/judge_tools.py` sends the lines below (after the marker) to the model in the `measure` tool's
description, and they are part of the tools judge's digest: a Watt release that changes an instrument is a new
digest once this file is regenerated.

<!-- ops -->
- angle(a, b, config, holds): The angle between the z axes of two frames (two faces' normals, an interface's dir), 0 to 180 deg: e.g. a = measure.angle(a=rest_face, b=world.desk.top).value
- bbox(of, frame, square_to, config, holds): The exact axis-aligned bounding box of an entity or a list of them: e.g. b = measure.bbox(of=base) # b.min, b.max, b.size: 3 lengths each
- bite(of, into, config, holds): How far `of` reaches radially past the surface of `into` (a bore, a cylindrical face): e.g. b = measure.bite(of=teeth, into=world.ceiling.hole) # b.least, b.most, b.pieces, b.area
- catches(of, behind, pull, wall, config, holds): What holds against a pull: e.g. c = measure.catches(of=barbs, behind=world.ceiling.back, wall=world.ceiling.bore, pull=-Z) # back: a contact interface (or a face such as world.ceiling.board.top); bore: a bore interface with d, decla
- clearance(a, b, exclude, config, holds): The signed clearance between two entities, the one distance reading: e.g. assert measure.clearance(a=hw.pi4, b=base, exclude=[posts]).value >= 0.5*mm
- edges(of, within, at, below, config, holds): The convex edges of a body inside a region, the reading W0729 judges a hand's or a digit's `edge` by: e.g. e = measure.edges(of=body_r, within=world.user.hand_r, at=grip_r) # e.sharpest >= 1.5*mm
- engagement(of, bolt, config, holds): How a thread is filled, as the fastener check reads it (`fasteners.engaged`). e.g. e = measure.engagement(of=board_screw_1) # e.length, e.bottom_gap
- frame(of, config, holds): The frame of an interface, a face, a feature or a placed item, in the assembly frame: e.g. assert measure.frame(of=hw.magnet.pole_a).z == -Z
- gap(a, b, config, holds): How far a frame's origin lies from a plane: e.g. assert measure.gap(a=hw.fan.intake, b=plate.bottom).value <= 0.05*mm
- interference(a, b, exclude, config, holds): The volume a and b share (0 mm3 when none), and its bounding box (None then). e.g. assert measure.interference(a=hw.ssd, b=frame).volume == 0*mm3
- made(of, along, config, holds): How a part is made, as the build records it: e.g. assert measure.made(of=plug).material == materials.tpu95a t = measure.made(of=plug, along=Z).tolerance # the +- of a size across its layers
- mass(of, config, holds): The mass, volume and centre of mass of entities: e.g. c = measure.mass(of=[stand, spool], holds={tilt: 30*deg}).centroid
- net(of, config, holds): The ports a net joins, and the cables whose conductors carry it, as entity ids (each equal to its handle, e.g. e.g. assert hw.fan.red in measure.net(of=v5).ports
- passage(of, config, holds): The clear section of an opening: e.g. gate = measure.passage(of=rear_exit) # gate.w, gate.h; vents = measure.passage(of=vents) # .area
- path(of, along, travel, against, flex, watch, samples, config, holds): Move bodies in a straight line and report what they run into. e.g. p = measure.path(of=hw.board, along=Z, travel=10*mm, against=[body, lid]) # p.hit, p.travel
- play(of, against, set, config, holds): How loosely the device holds an item (ADR-017 §2), the reading W0725 judges an `asm.captive` by: e.g. p = measure.play(of=world.phone) # p.along[0], p.turn[2], p.stops['+X'], p.open
- proud(of, beyond, within, config, holds): How far `of` stands beyond a planar face (`beyond`, a world item's screen, a camera bar's face) all round it: e.g. lip = measure.proud(of=case, beyond=world.phone.body.top, within=R.protect.band) # lip.least >= R.protect.lip
- ray(origin, dir, to, of, config, holds): Every crossing of a ray with the entities' surfaces, nearest first: e.g. h = measure.ray(origin=(0*mm, 0*mm, 50*mm), dir=-Z, of=[lid]) # h.hits[0].entity, .point
- seated(item, on, along, config, holds): How an item sits on its supports. e.g. s = measure.seated(item=world.hose, on=male, along=-Z) # s.depth, s.arc
- section(of, plane, at, normal, frame, config, holds): Cut entities with a plane and read each connected region of material. e.g. s = measure.section(of=hub, plane=Plane(origin=(0*mm, 0*mm, 4*mm), normal=Z)) # s.outlines[0].holes
- seen(of, by, config, holds): Where an entity lies as a viewer sees it (`by`, a world record of kind viewer): e.g. assert measure.seen(of=abxy, by=world.user).side == "right"
- tipping(on, push, force, g, config, holds): Whether a force tips the placed items over an edge of their footprint on a support plane. e.g. t = measure.tipping(on=world.desk.top, push=world.phone.tap, force=R.tap.f, g=R.tap.g)
- wall(of, below, config, holds): Thin material, read with the manufacturing checks' probe (`ops_process.thin_spots`): e.g. w = measure.wall(of=plug, below=machines.printer.min_wall) # w.thin_area, w.value
