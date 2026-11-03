#import "common.typ": setup, header
#show: setup
#set par(leading: 0.55em, spacing: 1.1em, justify: false)
#set text(size: 11pt)

#let d = json(bytes(sys.inputs.data))

#header(d)
#v(1.5em)

#d.date

#v(0.5em)
#d.greeting

#for p in d.paragraphs [
  #p

]

#d.sign_off \
#d.name
