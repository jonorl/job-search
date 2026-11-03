#import "common.typ": setup, header, section
#show: setup

#let d = json(bytes(sys.inputs.data))

#header(d)

#section("Summary")
#d.summary

#section("Technical Projects")
#for p in d.projects [
  #link(p.url, strong(p.name)) *(#p.stack.join(", "))*: #p.description \
]

#section("Technical Skills")
#for g in d.skills [
  *#g.group*: #g.items.join(", ") \
]

#section("Professional Experience")
#for (i, e) in d.experience.enumerate() {
  if i > 0 { v(0.3em) }
  grid(
    columns: (1fr, auto),
    [*#e.title,* #e.company – #e.location], [#e.start – #e.end],
  )
  v(-0.2em)
  list(marker: [•], indent: 0.2em, spacing: 0.4em, ..e.bullets.map(b => [#b]))
}

#section("Education")
#for ed in d.education {
  grid(columns: (1fr, auto), [*#ed.institution –* #ed.degree], [#ed.year])
}
