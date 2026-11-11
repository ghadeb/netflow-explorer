// This producer is built alongside GoFlow2's upstream builder for UDP 2055.
package builder

import (
	"bytes"
	"encoding/hex"
	"net/netip"
	"os"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/netsampler/goflow2/v2/decoders/netflow"
	"github.com/netsampler/goflow2/v2/producer"
	protoproducer "github.com/netsampler/goflow2/v2/producer/proto"
)

const applicationID = 95
const applicationName = 96

type applicationOption struct {
	Type     string `json:"type"`
	Router   string `json:"sampler_address"`
	Domain   uint32 `json:"observation_domain_id"`
	ID       string `json:"application_id"`
	Name     string `json:"application_name"`
	SeenAtNS int64  `json:"time_received_ns"`
}

type nbarProducer struct {
	producer.ProducerInterface
	allowed map[netip.Addr]bool
}

func newNBARProducer(cfg *protoproducer.ProducerConfig, defaults protoproducer.ProtoProducerConfig) (producer.ProducerInterface, error) {
	// Use the upstream field mapper so IDs stay attached to the decoded flow that carried them.
	const field = "application_id"
	cfg.Formatter.Fields = append(slices.Clone(defaults.GetFormatter().Fields()), field)
	cfg.Formatter.Protobuf = append(cfg.Formatter.Protobuf, protoproducer.ProtobufFormatterConfig{
		Name: field, Index: 1001, Type: "bytes",
	})
	cfg.NetFlowV9.Mapping = append(cfg.NetFlowV9.Mapping, protoproducer.NetFlowMapField{
		Type: applicationID, Destination: field,
	})
	mapped, err := cfg.Compile()
	if err != nil {
		return nil, err
	}
	base, err := protoproducer.CreateProtoProducer(mapped, protoproducer.CreateSamplingSystem)
	if err != nil {
		return nil, err
	}
	allowed := make(map[netip.Addr]bool)
	for _, value := range strings.Split(os.Getenv("FLOW_EXPORTERS"), ",") {
		if address, err := netip.ParseAddr(strings.TrimSpace(value)); err == nil {
			allowed[address.Unmap()] = true
		}
	}
	return &nbarProducer{ProducerInterface: base, allowed: allowed}, nil
}

func (p *nbarProducer) Produce(message any, args *producer.ProduceArgs) ([]producer.ProducerMessage, error) {
	router := args.SamplerAddress.Unmap()
	packet, ok := message.(*netflow.NFv9Packet)
	if !ok || !p.allowed[router] {
		return nil, nil
	}
	flows, err := p.ProducerInterface.Produce(packet, args)
	if err != nil {
		return flows, err
	}
	for _, set := range packet.FlowSets {
		options, ok := set.(netflow.OptionsDataFlowSet)
		if !ok {
			continue
		}
		for _, record := range options.Records {
			id, name := optionFields(record.OptionsValues)
			if id == "" || name == "" {
				continue
			}
			flows = append(flows, applicationOption{
				Type: "NETFLOW_V9_APPLICATION", Router: router.String(), Domain: packet.SourceId,
				ID: id, Name: name, SeenAtNS: args.TimeReceived.UnixNano(),
			})
		}
	}
	return flows, nil
}

func optionFields(fields []netflow.DataField) (id, name string) {
	for _, field := range fields {
		value, ok := field.Value.([]byte)
		if !ok {
			continue
		}
		switch field.Type {
		case applicationID:
			if len(value) >= 2 && len(value) <= 16 && value[0] != 0 {
				id = hex.EncodeToString(value)
			}
		case applicationName:
			label, _, _ := bytes.Cut(value, []byte{0})
			if len(label) <= 255 && utf8.Valid(label) {
				name = strings.TrimSpace(string(label))
			}
		}
	}
	return id, name
}

func (p *nbarProducer) Commit(messages []producer.ProducerMessage) {
	// Options are appended after the pooled GoFlow2 flow messages.
	for len(messages) > 0 {
		if _, ok := messages[len(messages)-1].(applicationOption); !ok {
			break
		}
		messages = messages[:len(messages)-1]
	}
	p.ProducerInterface.Commit(messages)
}
